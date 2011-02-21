# Copyright 2011 Andrei Savu <asavu@apache.org>
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os, sys
import zookeeper
import threading
import functools
import time

zookeeper.set_log_stream(sys.stdout)

ZOO_OPEN_ACL_UNSAFE = {"perms":0x1f, "scheme":"world", "id" :"anyone"};

def retry_on(*excepts):
    """ Retry function execution if some known exception types are raised """
    def _decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return fn(*args, **kwargs)
                except Exception, e:
                    if not any([isinstance(e, _) for _ in excepts]):
                        raise
                    # else: retry forever
                    time.sleep(0.5)
        return wrapper
    return _decorator

class ZooKeeper(object):
    """ Basic adapter; always retry on ConnectionLossException """

    def __init__(self, quorum):
        self._quorum = quorum

        self._handle = None
        self._connected = False

        self._cv = threading.Condition()
        self._connect()

    def _connect(self):
        """ Open a connection to the quorum and for it to be established """
        def watcher(handle, type, state, path):
            self._cv.acquire()
            self._connected = True

            self._cv.notify()
            self._cv.release()

        self._cv.acquire()
        self._handle = zookeeper.init(self._quorum, watcher, 10000)

        self._cv.wait(10.0)
        if not self._connected:
            print >>sys.stderr, 'Unable to connecto the ZooKeeper cluster.'
            sys.exit(-1)
        self._cv.release()

    def __getattr__(self, name):
        """ Pass-Through with connection handle and retry on ConnectionLossException """
        value = getattr(zookeeper, name)
        if callable(value):
            return functools.partial(
                retry_on(zookeeper.ConnectionLossException)(value), 
                self._handle
            )
        else:
            return name

    def ensure_exists(self, name, data = ''):
        try:
            self.create(name, data, [ZOO_OPEN_ACL_UNSAFE], 0)
        except zookeeper.NodeExistsException:
            pass # it's fine if the node already exists

class Producer(object):

    def __init__(self, zk):
        self._zk = zk

        self._zk.ensure_exists('/queue')
        self._zk.ensure_exists('/queue/items')

    @retry_on(zookeeper.NoNodeException)
    def put(self, data):
        name = self._zk.create("/queue/items/item-", "", 
            [ZOO_OPEN_ACL_UNSAFE], 
            zookeeper.SEQUENCE
        )
        return self._zk.set(name, data, 0)

class Consumer(object):

    def __init__(self, zk):
        self._zk = zk
        self._id = None

        map(self._zk.ensure_exists, ('/queue', '/queue/items', 
            '/queue/consumers', '/queue/partial'))
        self._register()

    def _register(self):
        self._id = self._zk.create("/queue/consumers/consumer-", '', 
            [ZOO_OPEN_ACL_UNSAFE], zookeeper.SEQUENCE)

        self._zk.create(self._fullpath('/active'), '',
            [ZOO_OPEN_ACL_UNSAFE], zookeeper.EPHEMERAL)
        self._zk.create(self._fullpath('/item'), '',
            [ZOO_OPEN_ACL_UNSAFE], 0)

    def _fullpath(self, sufix):
        return self._id + sufix

    def _move(self, src, dest):
        try:
            (data, stat) = self._zk.get(src, None)
            if data:
                self._zk.set(dest, data)
                self._zk.delete(src, stat['version'])
                return data

            elif stat['ctime'] < (time.time() - 300): # 5 minutes
                # a producer failed to enqueue an element
                # the consumer should just drop the empty item
                try:
                    self._zk.delete(src)
                except zookeeper.NoNodeException:
                    pass # someone else already removed the node
                return None

        except zookeeper.NoNodeException:
            # a consumer already reserved this znode
            self._zk.set(dest, '')
            return None

        except zookeeper.BadVersionException:
            # someone is modifying the queue in place. You can re-read or abort.
            raise

    def reserve(self, block = False):
        if block:
            return self._blocking_reserve()
        else:
            return self._simple_reserve()

    def _blocking_reserve(self):
        def queue_watcher(*args, **kwargs):
            self._zk._cv.acquire()
            self._zk._cv.notify()
            self._zk._cv.release()

        while True:
            self._zk._cv.acquire()
            children = sorted(self._zk.get_children('/queue/items', queue_watcher))
            for child in children:
                data = self._move('/queue/items/' + child, self._fullpath('/item'))
                if data:
                    self._zk._cv.release()
                    return data
                self._zk._cv.wait()
                self._zk._cv.release()

    def _simple_reserve(self):
        while True:
            children = sorted(self._zk.get_children('/queue/items', None))
            if len(children) == 0:
                return None
            for child in children:
                data = self._move('/queue/items/' + child, self._fullpath('/item'))
                if data: return data

    def done(self):
        self._zk.set(self._fullpath('/item'), '')

    def close(self):
        map(self._zk.delete, (self._fullpath('/item'), 
            self._fullpath('/active'), self._id))
        self._id = None

    def __repr__(self):
        return '<Consumer id=%r>' % self._id

class GarbageCollector(object):

    def __init__(self, zk):
        self._zk = zk

        map(self._zk.ensure_exists, ('/queue', 
            '/queue/consumers', '/queue/partial'))
 
    def collect(self):
        children = self._zk.get_children('/queue/consumers', None)
        for child in children:
            # XXX remove old inactive consumers
            break

if __name__ == '__main__':
    zk = ZooKeeper("localhost:2181,localhost:2182")

    p = Producer(zk)
    p.put('value-1')
    p.put('value-2')

    c = Consumer(zk)
    print c

    print c.reserve(block = True)
    c.done()

    print c.reserve()
    c.done()
    c.close()

    zk.close()
