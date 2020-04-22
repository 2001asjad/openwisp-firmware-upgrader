import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from django.utils import timezone
from paramiko.ssh_exception import NoValidConnectionsError, SSHException

from openwisp_controller.connection.connectors.exceptions import CommandFailedException
from openwisp_controller.connection.tests.base import SshServer

from ...upgraders.openwrt import OpenWrt
from . import TestUpgraderMixin, spy_mock

TEST_CHECKSUM = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'


def mocked_exec_upgrade_not_needed(command, exit_codes=None):
    cases = {
        'test -f /etc/openwisp/firmware_checksum': ['', 0],
        'cat /etc/openwisp/firmware_checksum': [TEST_CHECKSUM, 0],
    }
    return cases[command]


def mocked_exec_upgrade_success(command, exit_codes=None, timeout=None):
    defaults = ['', 0]
    cases = {
        'test -f /etc/openwisp/firmware_checksum': defaults,
        'cat /etc/openwisp/firmware_checksum': defaults,
        'sysupgrade --test /tmp/openwrt-ar71xx-generic-tl-wdr4300-v1-squashfs-sysupgrade.bin': defaults,
        'sysupgrade -v -c /tmp/openwrt-ar71xx-generic-tl-wdr4300-v1-squashfs-sysupgrade.bin': defaults,
        'mkdir -p /etc/openwisp': defaults,
        f'echo {TEST_CHECKSUM} > /etc/openwisp/firmware_checksum': defaults,
    }
    try:
        return cases[command]
    except KeyError:
        raise CommandFailedException()


def connect_fail_on_write_checksum_pre_action(*args, **kwargs):
    if connect_fail_on_write_checksum.mock.call_count >= 2:
        raise NoValidConnectionsError(errors={'127.0.0.1': 'mocked error'})


connect_fail_on_write_checksum = spy_mock(
    OpenWrt.connect, connect_fail_on_write_checksum_pre_action
)


class BaseTestOpenwrtUpgrader(TestUpgraderMixin):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mock_ssh_server = SshServer(
            {'root': cls._TEST_RSA_PRIVATE_KEY_PATH}
        ).__enter__()
        cls.ssh_server.port = cls.mock_ssh_server.port

    @classmethod
    def tearDownClass(cls):
        cls.mock_ssh_server.__exit__()

    def _trigger_upgrade(self, exception=None):
        ckey = self._create_credentials_with_key(port=self.ssh_server.port)
        device_conn = self._create_device_connection(credentials=ckey)
        build = self._create_build(organization=device_conn.device.organization)
        image = self._create_firmware_image(build=build)
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                device_fw = self._create_device_firmware(
                    image=image,
                    device=device_conn.device,
                    device_connection=False,
                    upgrade=True,
                )
        except Exception as e:
            if exception and isinstance(e, exception):
                device_fw = self.device_firmware_model.objects.order_by(
                    'created'
                ).last()
            else:
                raise e
        else:
            if exception:
                self.fail(f'{exception.__class__} not raised')
        device_conn.refresh_from_db()
        device_fw.refresh_from_db()
        self.assertEqual(device_fw.image.upgradeoperation_set.count(), 1)
        upgrade_op = device_fw.image.upgradeoperation_set.first()
        return device_fw, device_conn, upgrade_op, output

    @patch('scp.SCPClient.putfo')
    def test_image_test_failed(self, putfo_mocked):
        device_fw, device_conn, upgrade_op, output = self._trigger_upgrade()
        self.assertTrue(device_conn.is_working)
        putfo_mocked.assert_called_once()
        self.assertEqual(upgrade_op.status, 'aborted')
        self.assertIn('sysupgrade: not found', upgrade_op.log)
        self.assertFalse(device_fw.installed)

    @patch.object(
        OpenWrt, 'exec_command', side_effect=mocked_exec_upgrade_not_needed,
    )
    def test_upgrade_not_needed(self, mocked):
        device_fw, device_conn, upgrade_op, output = self._trigger_upgrade()
        self.assertTrue(device_conn.is_working)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(upgrade_op.status, 'success')
        self.assertIn('upgrade not needed', upgrade_op.log)
        self.assertTrue(device_fw.installed)

    @patch('scp.SCPClient.putfo')
    @patch.object(OpenWrt, 'SLEEP_TIME', 0)
    @patch.object(OpenWrt, 'RETRY_TIME', 0)
    @patch('billiard.Process.is_alive', return_value=True)
    @patch.object(OpenWrt, 'exec_command', side_effect=mocked_exec_upgrade_success)
    def test_upgrade_success(self, exec_command, is_alive, putfo):
        device_fw, device_conn, upgrade_op, output = self._trigger_upgrade()
        self.assertTrue(device_conn.is_working)
        # should be called 6 times but 1 time is
        # executed in a subprocess and not caught by mock
        self.assertEqual(exec_command.call_count, 5)
        self.assertEqual(putfo.call_count, 1)
        self.assertEqual(is_alive.call_count, 1)
        self.assertEqual(upgrade_op.status, 'success')
        lines = [
            'Image checksum file found',
            'Checksum different, proceeding',
            'Upgrade operation in progress',
            'Trying to reconnect to device (attempt n.1)',
            'Connected! Writing checksum',
            'Upgrade completed successfully',
        ]
        for line in lines:
            self.assertIn(line, upgrade_op.log)
        self.assertTrue(device_fw.installed)

    @patch('scp.SCPClient.putfo')
    @patch.object(OpenWrt, 'SLEEP_TIME', 0)
    @patch.object(OpenWrt, 'RETRY_TIME', 0)
    @patch.object(OpenWrt, 'exec_command', side_effect=mocked_exec_upgrade_success)
    @patch.object(OpenWrt, 'connect', connect_fail_on_write_checksum)
    def test_cant_reconnect_on_write_checksum(self, exec_command, putfo):
        start_time = timezone.now()
        with redirect_stderr(io.StringIO()):
            device_fw, device_conn, upgrade_op, output = self._trigger_upgrade()
        self.assertEqual(exec_command.call_count, 3)
        self.assertEqual(putfo.call_count, 1)
        self.assertEqual(connect_fail_on_write_checksum.mock.call_count, 11)
        self.assertEqual(upgrade_op.status, 'failed')
        lines = [
            'Checksum different, proceeding',
            'Upgrade operation in progress',
            'Trying to reconnect to device (attempt n.1)',
            'Device not reachable yet',
            'Giving up, device not reachable',
        ]
        for line in lines:
            self.assertIn(line, upgrade_op.log)
        self.assertTrue(device_fw.installed)
        self.assertFalse(device_conn.is_working)
        self.assertIn('Giving up', device_conn.failure_reason)
        self.assertTrue(device_conn.last_attempt > start_time)

    @patch('scp.SCPClient.putfo')
    @patch.object(OpenWrt, 'SLEEP_TIME', 0)
    @patch.object(OpenWrt, 'RETRY_TIME', 0)
    @patch.object(OpenWrt, 'exec_command', side_effect=mocked_exec_upgrade_success)
    @patch(
        'openwisp_controller.connection.connectors.openwrt.ssh.OpenWrt.connect',
        side_effect=Exception('Connection failed'),
    )
    def test_connection_failure(self, connect, exec_command, putfo):
        device_fw, device_conn, upgrade_op, output = self._trigger_upgrade(
            exception=RuntimeError
        )
        self.assertFalse(device_conn.is_working)
        self.assertEqual(exec_command.call_count, 0)
        self.assertEqual(putfo.call_count, 0)
        self.assertEqual(connect.call_count, 5)
        self.assertEqual(upgrade_op.status, 'failed')
        lines = [
            # 'Detected a recoverable failure: Connection failed.',
            # 'The upgrade operation will be retried soon.',
            'Max retries exceeded. Upgrade failed: Connection failed.'
        ]
        for line in lines:
            self.assertIn(line, upgrade_op.log)
        self.assertFalse(device_fw.installed)

    @patch.object(
        OpenWrt, 'upload', side_effect=SSHException('Invalid packet blocking')
    )
    @patch.object(OpenWrt, 'SLEEP_TIME', 0)
    @patch.object(OpenWrt, 'RETRY_TIME', 0)
    @patch.object(OpenWrt, 'exec_command', side_effect=mocked_exec_upgrade_success)
    def test_upload_failure(self, exec_command, upload):
        device_fw, device_conn, upgrade_op, output = self._trigger_upgrade(
            exception=RuntimeError
        )
        self.assertTrue(device_conn.is_working)
        self.assertEqual(upload.call_count, 5)
        self.assertEqual(upgrade_op.status, 'failed')
        lines = [
            'Image checksum file found',
            'Checksum different, proceeding',
            # 'Detected a recoverable failure: Invalid packet blocking.',
            # 'The upgrade operation will be retried soon.',
            'Max retries exceeded. Upgrade failed: Invalid packet blocking.',
        ]
        for line in lines:
            self.assertIn(line, upgrade_op.log)
        self.assertFalse(device_fw.installed)
