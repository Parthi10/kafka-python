import copy
import heapq
import itertools
import logging
import random
import select
import time

import six

import kafka.common as Errors # TODO: make Errors a separate class

from .cluster import ClusterMetadata
from .conn import BrokerConnection, ConnectionStates, collect_hosts
from .future import Future
from .protocol.metadata import MetadataRequest
from .protocol.produce import ProduceRequest
from .version import __version__

log = logging.getLogger(__name__)


class KafkaClient(object):
    """
    A network client for asynchronous request/response network i/o.
    This is an internal class used to implement the
    user-facing producer and consumer clients.

    This class is not thread-safe!
    """
    DEFAULT_CONFIG = {
        'bootstrap_servers': 'localhost',
        'client_id': 'kafka-python-' + __version__,
        'request_timeout_ms': 40000,
        'reconnect_backoff_ms': 50,
        'max_in_flight_requests_per_connection': 5,
        'receive_buffer_bytes': 32768,
        'send_buffer_bytes': 131072,
    }

    def __init__(self, **configs):
        """Initialize an asynchronous kafka client

        Keyword Arguments:
            bootstrap_servers: 'host[:port]' string (or list of 'host[:port]'
                strings) that the consumer should contact to bootstrap initial
                cluster metadata. This does not have to be the full node list.
                It just needs to have at least one broker that will respond to a
                Metadata API Request. Default port is 9092. If no servers are
                specified, will default to localhost:9092.
            client_id (str): a name for this client. This string is passed in
                each request to servers and can be used to identify specific
                server-side log entries that correspond to this client. Also
                submitted to GroupCoordinator for logging with respect to
                consumer group administration. Default: 'kafka-python-{version}'
            request_timeout_ms (int): Client request timeout in milliseconds.
                Default: 40000.
            reconnect_backoff_ms (int): The amount of time in milliseconds to
                wait before attempting to reconnect to a given host.
                Default: 50.
            max_in_flight_requests_per_connection (int): Requests are pipelined
                to kafka brokers up to this number of maximum requests per
                broker connection. Default: 5.
            send_buffer_bytes (int): The size of the TCP send buffer
                (SO_SNDBUF) to use when sending data. Default: 131072
            receive_buffer_bytes (int): The size of the TCP receive buffer
                (SO_RCVBUF) to use when reading data. Default: 32768
        """
        self.config = copy.copy(self.DEFAULT_CONFIG)
        for key in self.config:
            if key in configs:
                self.config[key] = configs[key]

        self.cluster = ClusterMetadata(**self.config)
        self._topics = set() # empty set will fetch all topic metadata
        self._metadata_refresh_in_progress = False
        self._conns = {}
        self._connecting = set()
        self._delayed_tasks = DelayedTaskQueue()
        self._last_bootstrap = 0
        self._bootstrap_fails = 0
        self._bootstrap(collect_hosts(self.config['bootstrap_servers']))

    def _bootstrap(self, hosts):
        # Exponential backoff if bootstrap fails
        backoff_ms = self.config['reconnect_backoff_ms'] * 2 ** self._bootstrap_fails
        next_at = self._last_bootstrap + backoff_ms / 1000.0
        now = time.time()
        if next_at > now:
            log.debug("Sleeping %0.4f before bootstrapping again", next_at - now)
            time.sleep(next_at - now)
        self._last_bootstrap = time.time()

        metadata_request = MetadataRequest([])
        for host, port in hosts:
            log.debug("Attempting to bootstrap via node at %s:%s", host, port)
            bootstrap = BrokerConnection(host, port, **self.config)
            bootstrap.connect()
            while bootstrap.state is ConnectionStates.CONNECTING:
                bootstrap.connect()
            if bootstrap.state is not ConnectionStates.CONNECTED:
                bootstrap.close()
                continue
            future = bootstrap.send(metadata_request)
            while not future.is_done:
                bootstrap.recv()
            if future.failed():
                bootstrap.close()
                continue
            self.cluster.update_metadata(future.value)

            # A cluster with no topics can return no broker metadata
            # in that case, we should keep the bootstrap connection
            if not len(self.cluster.brokers()):
                self._conns['bootstrap'] = bootstrap
            self._bootstrap_fails = 0
            break
        # No bootstrap found...
        else:
            log.error('Unable to bootstrap from %s', hosts)
            # Max exponential backoff is 2^12, x4000 (50ms -> 200s)
            self._bootstrap_fails = min(self._bootstrap_fails + 1, 12)

    def _can_connect(self, node_id):
        if node_id not in self._conns:
            if self.cluster.broker_metadata(node_id):
                return True
            return False
        conn = self._conns[node_id]
        return conn.state is ConnectionStates.DISCONNECTED and not conn.blacked_out()

    def _initiate_connect(self, node_id):
        """Initiate a connection to the given node (must be in metadata)"""
        if node_id not in self._conns:
            broker = self.cluster.broker_metadata(node_id)
            assert broker, 'Broker id %s not in current metadata' % node_id

            log.debug("Initiating connection to node %s at %s:%s",
                      node_id, broker.host, broker.port)
            self._conns[node_id] = BrokerConnection(broker.host, broker.port,
                                                    **self.config)
        return self._finish_connect(node_id)

    def _finish_connect(self, node_id):
        assert node_id in self._conns, '%s is not in current conns' % node_id
        state = self._conns[node_id].connect()
        if state is ConnectionStates.CONNECTING:
            self._connecting.add(node_id)
        elif node_id in self._connecting:
            log.debug("Node %s connection state is %s", node_id, state)
            self._connecting.remove(node_id)
        return state

    def ready(self, node_id):
        """Check whether a node is connected and ok to send more requests.

        Arguments:
            node_id (int): the id of the node to check

        Returns:
            bool: True if we are ready to send to the given node
        """
        if self.is_ready(node_id):
            return True

        if self._can_connect(node_id):
            # if we are interested in sending to a node
            # and we don't have a connection to it, initiate one
            self._initiate_connect(node_id)

        if node_id in self._connecting:
            self._finish_connect(node_id)

        return self.is_ready(node_id)

    def close(self, node_id=None):
        """Closes the connection to a particular node (if there is one).

        Arguments:
            node_id (int): the id of the node to close
        """
        if node_id is None:
            for conn in self._conns.values():
                conn.close()
        elif node_id in self._conns:
            self._conns[node_id].close()
        else:
            log.warning("Node %s not found in current connection list; skipping", node_id)
            return

    def is_disconnected(self, node_id):
        """Check whether the node connection has been disconnected failed.

        A disconnected node has either been closed or has failed. Connection
        failures are usually transient and can be resumed in the next ready()
        call, but there are cases where transient failures need to be caught
        and re-acted upon.

        Arguments:
            node_id (int): the id of the node to check

        Returns:
            bool: True iff the node exists and is disconnected
        """
        if node_id not in self._conns:
            return False
        return self._conns[node_id].state is ConnectionStates.DISCONNECTED

    def is_ready(self, node_id):
        """Check whether a node is ready to send more requests.

        In addition to connection-level checks, this method also is used to
        block additional requests from being sent during a metadata refresh.

        Arguments:
            node_id (int): id of the node to check

        Returns:
            bool: True if the node is ready and metadata is not refreshing
        """
        # if we need to update our metadata now declare all requests unready to
        # make metadata requests first priority
        if not self._metadata_refresh_in_progress and not self.cluster.ttl() == 0:
            if self._can_send_request(node_id):
                return True
        return False

    def _can_send_request(self, node_id):
        if node_id not in self._conns:
            return False
        conn = self._conns[node_id]
        return conn.connected() and conn.can_send_more()

    def send(self, node_id, request):
        """Send a request to a specific node.

        Arguments:
            node_id (int): destination node
            request (Struct): request object (not-encoded)

        Raises:
            NodeNotReadyError: if node_id is not ready

        Returns:
            Future: resolves to Response struct
        """
        if not self._can_send_request(node_id):
            raise Errors.NodeNotReadyError("Attempt to send a request to node"
                                           " which is not ready (node id %s)."
                                           % node_id)

        # Every request gets a response, except one special case:
        expect_response = True
        if isinstance(request, ProduceRequest) and request.required_acks == 0:
            expect_response = False

        return self._conns[node_id].send(request, expect_response=expect_response)

    def poll(self, timeout_ms=None, future=None):
        """Try to read and write to sockets.

        This method will also attempt to complete node connections, refresh
        stale metadata, and run previously-scheduled tasks.

        Arguments:
            timeout_ms (int, optional): maximum amount of time to wait (in ms)
                for at least one response. Must be non-negative. The actual
                timeout will be the minimum of timeout, request timeout and
                metadata timeout. Default: request_timeout_ms
            future (Future, optional): if provided, blocks until future.is_done

        Returns:
            list: responses received (can be empty)
        """
        if timeout_ms is None:
            timeout_ms = self.config['request_timeout_ms']

        responses = []

        # Loop for futures, break after first loop if None
        while True:

            # Attempt to complete pending connections
            for node_id in list(self._connecting):
                self._finish_connect(node_id)

            # Send a metadata request if needed
            metadata_timeout = self._maybe_refresh_metadata()

            # Send scheduled tasks
            for task, future in self._delayed_tasks.pop_ready():
                try:
                    result = task()
                except Exception as e:
                    log.error("Task %s failed: %s", task, e)
                    future.failure(e)
                else:
                    future.success(result)

            timeout = min(timeout_ms, metadata_timeout,
                          self.config['request_timeout_ms'])
            timeout /= 1000.0

            responses.extend(self._poll(timeout))
            if not future or future.is_done:
                break

        return responses

    def _poll(self, timeout):
        # select on reads across all connected sockets, blocking up to timeout
        sockets = dict([(conn._sock, conn)
                        for conn in six.itervalues(self._conns)
                        if (conn.state is ConnectionStates.CONNECTED
                            and conn.in_flight_requests)])
        if not sockets:
            return []

        ready, _, _ = select.select(list(sockets.keys()), [], [], timeout)

        responses = []
        # list, not iterator, because inline callbacks may add to self._conns
        for sock in ready:
            conn = sockets[sock]
            response = conn.recv() # Note: conn.recv runs callbacks / errbacks
            if response:
                responses.append(response)
        return responses

    def in_flight_request_count(self, node_id=None):
        """Get the number of in-flight requests for a node or all nodes.

        Arguments:
            node_id (int, optional): a specific node to check. If unspecified,
                return the total for all nodes

        Returns:
            int: pending in-flight requests for the node, or all nodes if None
        """
        if node_id is not None:
            if node_id not in self._conns:
                return 0
            return len(self._conns[node_id].in_flight_requests)
        else:
            return sum([len(conn.in_flight_requests) for conn in self._conns.values()])

    def least_loaded_node(self):
        """Choose the node with fewest outstanding requests, with fallbacks.

        This method will prefer a node with an existing connection, but will
        potentially choose a node for which we don't yet have a connection if
        all existing connections are in use. This method will never choose a
        node that was disconnected within the reconnect backoff period.
        If all else fails, the method will attempt to bootstrap again using the
        bootstrap_servers list.

        Returns:
            node_id or None if no suitable node was found
        """
        nodes = list(self._conns.keys())
        random.shuffle(nodes)
        inflight = float('inf')
        found = None
        for node_id in nodes:
            conn = self._conns[node_id]
            curr_inflight = len(conn.in_flight_requests)
            if curr_inflight == 0 and conn.connected():
                # if we find an established connection with no in-flight requests we can stop right away
                return node_id
            elif not conn.blacked_out() and curr_inflight < inflight:
                # otherwise if this is the best we have found so far, record that
                inflight = curr_inflight
                found = node_id

        if found is not None:
            return found

        # if we found no connected node, return a disconnected one
        log.debug("No connected nodes found. Trying disconnected nodes.")
        for node_id in nodes:
            if not self._conns[node_id].blacked_out():
                return node_id

        # if still no luck, look for a node not in self._conns yet
        log.debug("No luck. Trying all broker metadata")
        for broker in self.cluster.brokers():
            if broker.nodeId not in self._conns:
                return broker.nodeId

        # Last option: try to bootstrap again
        log.error('No nodes found in metadata -- retrying bootstrap')
        self._bootstrap(collect_hosts(self.config['bootstrap_servers']))
        return None

    def set_topics(self, topics):
        """Set specific topics to track for metadata.

        Arguments:
            topics (list of str): topics to check for metadata

        Returns:
            Future: resolves after metadata request/response
        """
        if set(topics).difference(self._topics):
            future = self.cluster.request_update()
        else:
            future = Future().success(set(topics))
        self._topics = set(topics)
        return future

    # request metadata update on disconnect and timedout
    def _maybe_refresh_metadata(self):
        """Send a metadata request if needed.

        Returns:
            int: milliseconds until next refresh
        """
        ttl = self.cluster.ttl()
        if ttl > 0:
            return ttl

        if self._metadata_refresh_in_progress:
            return 9999999999

        node_id = self.least_loaded_node()

        if self._can_send_request(node_id):
            request = MetadataRequest(list(self._topics))
            log.debug("Sending metadata request %s to node %s", request, node_id)
            future = self.send(node_id, request)
            future.add_callback(self.cluster.update_metadata)
            future.add_errback(self.cluster.failed_update)

            self._metadata_refresh_in_progress = True
            def refresh_done(val_or_error):
                self._metadata_refresh_in_progress = False
            future.add_callback(refresh_done)
            future.add_errback(refresh_done)

        elif self._can_connect(node_id):
            log.debug("Initializing connection to node %s for metadata request", node_id)
            self._initiate_connect(node_id)

        return 0

    def schedule(self, task, at):
        """Schedule a new task to be executed at the given time.

        This is "best-effort" scheduling and should only be used for coarse
        synchronization. A task cannot be scheduled for multiple times
        simultaneously; any previously scheduled instance of the same task
        will be cancelled.

        Arguments:
            task (callable): task to be scheduled
            at (float or int): epoch seconds when task should run

        Returns:
            Future: resolves to result of task call, or exception if raised
        """
        return self._delayed_tasks.add(task, at)

    def unschedule(self, task):
        """Unschedule a task.

        This will remove all instances of the task from the task queue.
        This is a no-op if the task is not scheduled.

        Arguments:
            task (callable): task to be unscheduled
        """
        self._delayed_tasks.remove(task)

    def check_version(self, node_id=None):
        """Attempt to guess the broker version"""
        if node_id is None:
            node_id = self.least_loaded_node()

        def connect():
            timeout = time.time() + 10
            # brokers < 0.9 do not return any broker metadata if there are no topics
            # so we're left with a single bootstrap connection
            while not self.ready(node_id):
                if time.time() >= timeout:
                    raise Errors.NodeNotReadyError(node_id)
                time.sleep(0.025)

        # kafka kills the connection when it doesnt recognize an API request
        # so we can send a test request and then follow immediately with a
        # vanilla MetadataRequest. If the server did not recognize the first
        # request, both will be failed with a ConnectionError that wraps
        # socket.error (32 or 54)
        import socket
        from .protocol.admin import ListGroupsRequest
        from .protocol.commit import (
            OffsetFetchRequest_v0, GroupCoordinatorRequest)
        from .protocol.metadata import MetadataRequest

        test_cases = [
            ('0.9', ListGroupsRequest()),
            ('0.8.2', GroupCoordinatorRequest('kafka-python-default-group')),
            ('0.8.1', OffsetFetchRequest_v0('kafka-python-default-group', [])),
            ('0.8.0', MetadataRequest([])),
        ]


        for version, request in test_cases:
            connect()
            f = self.send(node_id, request)
            time.sleep(0.5)
            self.send(node_id, MetadataRequest([]))
            self.poll(future=f)

            assert f.is_done

            if f.succeeded():
                log.info('Broker version identifed as %s', version)
                return version

            assert isinstance(f.exception.message, socket.error)
            assert f.exception.message.errno in (32, 54)
            log.info("Broker is not v%s -- it did not recognize %s",
                     version, request.__class__.__name__)
            continue


class DelayedTaskQueue(object):
    # see https://docs.python.org/2/library/heapq.html
    def __init__(self):
        self._tasks = [] # list of entries arranged in a heap
        self._task_map = {} # mapping of tasks to entries
        self._counter = itertools.count() # unique sequence count

    def add(self, task, at):
        """Add a task to run at a later time.

        Arguments:
            task: can be anything, but generally a callable
            at (float or int): epoch seconds to schedule task

        Returns:
            Future: a future that will be returned with the task when ready
        """
        if task in self._task_map:
            self.remove(task)
        count = next(self._counter)
        future = Future()
        entry = [at, count, (task, future)]
        self._task_map[task] = entry
        heapq.heappush(self._tasks, entry)
        return future

    def remove(self, task):
        """Remove a previously scheduled task.

        Raises:
            KeyError: if task is not found
        """
        entry = self._task_map.pop(task)
        task, future = entry[-1]
        future.failure(Errors.Cancelled)
        entry[-1] = 'REMOVED'

    def _drop_removed(self):
        while self._tasks and self._tasks[0][-1] is 'REMOVED':
            at, count, task = heapq.heappop(self._tasks)

    def _pop_next(self):
        self._drop_removed()
        if not self._tasks:
            raise KeyError('pop from an empty DelayedTaskQueue')
        _, _, maybe_task = heapq.heappop(self._tasks)
        if maybe_task is 'REMOVED':
            raise ValueError('popped a removed tasks from queue - bug')
        else:
            task, future = maybe_task
        del self._task_map[task]
        return (task, future)

    def next_at(self):
        """Number of seconds until next task is ready."""
        self._drop_removed()
        if not self._tasks:
            return 9999999999
        else:
            return max(self._tasks[0][0] - time.time(), 0)

    def pop_ready(self):
        """Pop and return a list of all ready (task, future) tuples"""
        ready_tasks = []
        while self._tasks and self._tasks[0][0] < time.time():
            try:
                task = self._pop_next()
            except KeyError:
                break
            ready_tasks.append(task)
        return ready_tasks
