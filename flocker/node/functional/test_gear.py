# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""Functional tests for :module:`flocker.node.gear`."""

import os
import json
import subprocess
import socket
import time
from unittest import skipIf

from twisted.trial.unittest import TestCase
from twisted.python.procutils import which
from twisted.internet.defer import succeed
from twisted.internet.error import ConnectionRefusedError

from treq import request, content

from characteristic import attributes

from ...testtools import loop_until, find_free_port

from ..test.test_gear import make_igearclient_tests, random_name
from ..gear import GearClient, GearError, GEAR_PORT, PortMap


def _gear_running():
    """Return whether gear is running on this machine.

    :return: ``True`` if gear can be reached, otherwise ``False``.
    """
    if not which("gear"):
        return False
    sock = socket.socket()
    try:
        return not sock.connect_ex((b'127.0.0.1', GEAR_PORT))
    finally:
        sock.close()
_if_gear_configured = skipIf(not _gear_running(),
                             "Must run on machine with `gear daemon` running.")
_if_root = skipIf(os.getuid() != 0, "Must run as root.")


class IGearClientTests(make_igearclient_tests(
        lambda test_case: GearClient("127.0.0.1"))):
    """``IGearClient`` tests for ``FakeGearClient``."""

    @_if_gear_configured
    def setUp(self):
        pass


class GearClientTests(TestCase):
    """Implementation-specific tests for ``GearClient``."""

    @_if_gear_configured
    def setUp(self):
        pass

    def start_container(self, unit_name,
                        image_name=u"openshift/busybox-http-app",
                        ports=None, links=None):
        """Start a unit and wait until it's up and running.

        :param unicode unit_name: See ``IGearClient.add``.
        :param unicode image_name: See ``IGearClient.add``.
        :param list ports: See ``IGearClient.add``.
        :param list links: See ``IGearClient.add``.

        :return: Deferred that fires when the unit is running.
        """
        client = GearClient("127.0.0.1")
        d = client.add(
            unit_name=unit_name,
            image_name=image_name,
            ports=ports,
            links=links,
        )
        self.addCleanup(client.remove, unit_name)

        def is_started(data):
            return [container for container in data[u"Containers"] if
                    (container[u"Id"] == unit_name and
                     container[u"SubState"] == u"running")]

        def check_if_started():
            # Replace with ``GearClient.list`` as part of
            # https://github.com/ClusterHQ/flocker/issues/32
            responded = request(
                b"GET", b"http://127.0.0.1:%d/containers" % (GEAR_PORT,),
                persistent=False)
            responded.addCallback(content)
            responded.addCallback(json.loads)
            responded.addCallback(is_started)
            return responded

        def added(_):
            return loop_until(check_if_started)
        d.addCallback(added)
        return d

    def test_add_starts_container(self):
        """``GearClient.add`` starts the container."""
        name = random_name()
        return self.start_container(name)

    @_if_root
    def test_correct_image_used(self):
        """``GearClient.add`` creates a container with the specified image."""
        name = random_name()
        d = self.start_container(name)

        def started(_):
            data = subprocess.check_output(
                [b"docker", b"inspect", name.encode("ascii")])
            self.assertEqual(json.loads(data)[0][u"Config"][u"Image"],
                             u"openshift/busybox-http-app")
        d.addCallback(started)
        return d

    def test_exists_error(self):
        """``GearClient.exists`` returns ``Deferred`` that errbacks with
        ``GearError`` if response code is unexpected.
        """
        client = GearClient("127.0.0.1")
        # Illegal container name should make gear complain when we check
        # if it exists:
        d = client.exists(u"!!##!!")
        return self.assertFailure(d, GearError)

    def test_add_error(self):
        """``GearClient.add`` returns ``Deferred`` that errbacks with
        ``GearError`` if response code is not a success response code.
        """
        client = GearClient("127.0.0.1")
        # add() calls exists(), and we don't want exists() to be the one
        # failing since that's not the code path we're testing, so bypass
        # it:
        client.exists = lambda _: succeed(False)
        # Illegal container name should make gear complain when we try to
        # install the container:
        d = client.add(u"!!!###!!!", u"busybox")
        return self.assertFailure(d, GearError)

    def test_remove_error(self):
        """``GearClient.remove`` returns ``Deferred`` that errbacks with
        ``GearError`` if response code is not a success response code.
        """
        client = GearClient("127.0.0.1")
        # Illegal container name should make gear complain when we try to
        # remove it:
        d = client.remove(u"!!##!!")
        return self.assertFailure(d, GearError)

    def request_until_response(self, port):
        """
        Resend a test HTTP request until a response is received.

        The container may have started, but the webserver inside may take a
        little while to start serving requests.

        :param int port: The localhost port to which an HTTP request will be
            sent.

        :return: A ``Deferred`` which fires with the result of the first
            successful HTTP request.
        """
        def send_request():
            """
            Send an HTTP request in a loop until the request is answered.
            """
            response = request(
                b"GET", b"http://127.0.0.1:%d" % (port,),
                persistent=False)

            def check_error(failure):
                """
                Catch ConnectionRefused errors and return False so that
                loop_until repeats the request.

                Other error conditions will be passed down the errback chain.
                """
                failure.trap(ConnectionRefusedError)
                return False
            response.addErrback(check_error)
            return response

        return loop_until(send_request)

    def test_add_with_port(self):
        """
        GearClient.add accepts a ports argument which is passed to gear to
        expose those ports on the unit.

        Assert that the busybox-http-app returns the expected "Hello world!"
        response.

        XXX: We should use a stable internal container instead. See
        https://github.com/hybridlogic/flocker/issues/120

        XXX: The busybox-http-app returns headers in the body of its response,
        hence this over complicated custom assertion. See
        https://github.com/openshift/geard/issues/213
        """
        expected_response = b'Hello world!\n'
        external_port = find_free_port()[1]
        name = random_name()
        d = self.start_container(
            name, ports=[PortMap(internal_address='1.2.3.4', internal_port=8080, external_port=external_port)])

        d.addCallback(
            lambda ignored: self.request_until_response(external_port))

        def started(response):
            d = content(response)
            d.addCallback(lambda body: self.assertIn(expected_response, body))
            return d
        d.addCallback(started)

        return d

    def _first_non_loopback_address(self):
        """
        Return an IPv4 address found in system configuration.

        :return: An ``IPv4Address`` address the machine is configured with.
        """
        from netifaces import interfaces, ifaddresses, AF_INET

        for interface in interfaces():
            for link in ifaddresses(interface)[AF_INET]:
                if link['addr'] != b'127.0.0.1':
                    return link['addr']

    def test_add_with_links(self):
        """
        GearClient.add accepts a links argument which sets up links between
        container local ports and host local ports.
        """
        internal_port = 31337
        image_name = b'flocker/send_xxx_to_31337'
        # Create a Docker image
        image = DockerImageBuilder(
            docker_dir=os.path.dirname(__file__),
            tag=image_name
        )
        image.build()
#        self.addCleanup(image.remove)

        # This is the target of the proxy which will be created.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setblocking(0)
        address = self._first_non_loopback_address()
        server.bind((address, 0))
        server.listen(1)
        host_ip, host_port = server.getsockname()[:2]
        name = random_name()
        d = self.start_container(
            unit_name=name,
            image_name=image_name,
            links=[PortMap(internal_address=address, internal_port=internal_port, external_port=host_port)]
        )

        def started(ignored):
            time.sleep(5)
            accepted, client_address = server.accept()
            self.assertEqual(b'xxx\n', accepted.recv(1024))
        d.addCallback(started)

        return d


@attributes(['docker_dir', 'tag'])
class DockerImageBuilder(object):
    def build(self):
        command = [
            b'docker', b'build',
            b'--force-rm',
            b'--tag=%s' % (self.tag,),
            self.docker_dir
        ]
        subprocess.check_call(command)

    def remove(self):
        command = [
            b'docker', b'rmi',
            b'--force',
            self.tag
        ]
        subprocess.check_call(command)
