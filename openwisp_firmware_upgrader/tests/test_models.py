import os

import mock
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from openwisp_controller.connection.tests.base import CreateConnectionsMixin

from ..hardware import FIRMWARE_IMAGE_MAP
from ..models import Build, Category, DeviceFirmware, FirmwareImage, UpgradeOperation


class TestUpgraderMixin(CreateConnectionsMixin):
    FAKE_IMAGE_PATH = os.path.join(settings.MEDIA_ROOT, 'fake-img.bin')
    TPLINK_4300_IMAGE = 'ar71xx-generic-tl-wdr4300-v1-squashfs-sysupgrade.bin'
    TPLINK_4300_IL_IMAGE = 'ar71xx-generic-tl-wdr4300-v1-il-squashfs-sysupgrade.bin'

    def tearDown(self):
        for fw in FirmwareImage.objects.all():
            fw.delete()

    def _create_category(self, **kwargs):
        opts = dict(name='Test Category')
        opts.update(kwargs)
        if 'organization' not in opts:
            opts['organization'] = self._create_org()
        c = Category(**opts)
        c.full_clean()
        c.save()
        return c

    def _create_build(self, **kwargs):
        opts = dict(version='0.1')
        opts.update(kwargs)
        if 'category' not in opts:
            opts['category'] = self._create_category()
        if 'organization' not in opts:
            opts['organization'] = opts['category'].organization
        b = Build(**opts)
        b.full_clean()
        b.save()
        return b

    def _create_firmware_image(self, **kwargs):
        opts = dict(type=self.TPLINK_4300_IMAGE)
        opts.update(kwargs)
        if 'build' not in opts:
            opts['build'] = self._create_build()
        if 'organization' not in opts:
            opts['organization'] = opts['build'].organization
        if 'file' not in opts:
            opts['file'] = self._get_simpleuploadedfile()
        fw = FirmwareImage(**opts)
        fw.full_clean()
        fw.save()
        return fw

    def _get_simpleuploadedfile(self):
        with open(self.FAKE_IMAGE_PATH, 'rb') as f:
            image = f.read()
        return SimpleUploadedFile(name='uploaded-fake-image.bin',
                                  content=image,
                                  content_type='text/plain')

    def _create_device_firmware(self, upgrade=False, device_connection=True, **kwargs):
        opts = dict()
        opts.update(kwargs)
        if 'image' not in opts:
            opts['image'] = self._create_firmware_image()
        if 'device' not in opts:
            opts['device'] = self._create_device(organization=opts['image'].organization)
            self._create_config(device=opts['device'])
        if device_connection:
            self._create_device_connection(device=opts['device'])
        device_fw = DeviceFirmware(**opts)
        device_fw.full_clean()
        device_fw.save(upgrade=upgrade)
        return device_fw


class TestModels(TestUpgraderMixin, TestCase):
    def test_category_str(self):
        c = Category(name='WiFi Hotspot')
        self.assertEqual(str(c), c.name)

    def test_build_str(self):
        c = self._create_category()
        b = Build(category=c, version='0.1')
        self.assertIn(c.name, str(b))
        self.assertIn(b.version, str(b))

    def test_build_str_no_category(self):
        b = Build()
        self.assertIsNotNone(str(b))

    def test_fw_str(self):
        fw = self._create_firmware_image()
        self.assertIn(str(fw.build), str(fw))
        self.assertIn(fw.file.name, str(fw))

    def test_fw_str_new(self):
        fw = FirmwareImage()
        self.assertIsNotNone(str(fw))

    @mock.patch('openwisp_firmware_upgrader.models.UpgradeOperation.upgrade', return_value=None)
    def test_device_fw_image_changed(self, *args):
        device_fw = DeviceFirmware()
        self.assertIsNone(device_fw._old_image)
        # save
        device_fw = self._create_device_firmware(upgrade=False)
        self.assertEqual(device_fw._old_image, device_fw.image)
        self.assertEqual(UpgradeOperation.objects.count(), 0)
        # init
        device_fw = DeviceFirmware.objects.first()
        self.assertEqual(device_fw._old_image, device_fw.image)
        # change
        build2 = self._create_build(category=device_fw.image.build.category,
                                    version='0.2')
        fw2 = self._create_firmware_image(build=build2, type=device_fw.image.type)
        old_image = device_fw.image
        device_fw.image = fw2
        self.assertNotEqual(device_fw._old_image, device_fw.image)
        self.assertEqual(device_fw._old_image, old_image)
        device_fw.full_clean()
        device_fw.save()
        self.assertEqual(UpgradeOperation.objects.count(), 1)

    @mock.patch('openwisp_firmware_upgrader.models.UpgradeOperation.upgrade', return_value=None)
    def test_device_fw_created(self, *args):
        self._create_device_firmware(upgrade=True)
        self.assertEqual(UpgradeOperation.objects.count(), 1)

    def test_device_fw_no_connection(self):
        try:
            self._create_device_firmware(device_connection=False)
        except ValidationError as e:
            self.assertIn('related connection', str(e))
        else:
            self.fail('ValidationError not raised')

    def test_invalid_board(self):
        image = FIRMWARE_IMAGE_MAP['ar71xx-generic-tl-wdr4300-v1-squashfs-sysupgrade.bin']
        boards = image['boards']
        del image['boards']
        err = None
        try:
            self._create_firmware_image()
        except ValidationError as e:
            err = e
        image['boards'] = boards
        if err:
            self.assertIn('type', err.message_dict)
            self.assertIn('not find boards', str(err))
        else:
            self.fail('ValidationError not raised')

    def test_device_firmware_image_invalid_model(self):
        device_fw = self._create_device_firmware()
        different_img = self._create_firmware_image(
            type=self.TPLINK_4300_IL_IMAGE,
            organization=device_fw.device.organization
        )
        try:
            device_fw.image = different_img
            device_fw.full_clean()
        except ValidationError as e:
            self.assertIn('model do not match', str(e))
        else:
            self.fail('ValidationError not raised')

    def _create_upgrade_env(self, device_firmware=True):
        org = self._create_org()
        category = self._create_category(organization=org)
        build1 = self._create_build(category=category, version='0.1')
        image1a = self._create_firmware_image(build=build1, type=self.TPLINK_4300_IMAGE)
        image1b = self._create_firmware_image(build=build1, type=self.TPLINK_4300_IL_IMAGE)
        # create devices
        d1 = self._create_device(name='device1', organization=org,
                                 mac_address='00:22:bb:33:cc:44',
                                 model=image1a.boards[0])
        d2 = self._create_device(name='device2', organization=org,
                                 mac_address='00:11:bb:22:cc:33',
                                 model=image1b.boards[0])
        ssh_credentials = self._create_credentials()
        self._create_config(device=d1)
        self._create_config(device=d2)
        self._create_device_connection(device=d1, credentials=ssh_credentials)
        self._create_device_connection(device=d2, credentials=ssh_credentials)
        # create device firmware (optional)
        if device_firmware:
            self._create_device_firmware(device=d1, image=image1a, device_connection=False)
            self._create_device_firmware(device=d2, image=image1b, device_connection=False)
        # create a new firmware build
        build2 = self._create_build(category=category, version='0.2')
        image2a = self._create_firmware_image(build=build2, type=self.TPLINK_4300_IMAGE)
        image2b = self._create_firmware_image(build=build2, type=self.TPLINK_4300_IL_IMAGE)
        data = {
            'build2': build2,
            'd1': d1,
            'd2': d2,
            'image1a': image1a,
            'image1b': image1b,
            'image2a': image2a,
            'image2b': image2b
        }
        return data

    def test_upgrade_related_devices(self):
        env = self._create_upgrade_env()
        # check everything is as expected
        self.assertEqual(UpgradeOperation.objects.count(), 0)
        self.assertEqual(env['d1'].devicefirmware.image, env['image1a'])
        self.assertEqual(env['d2'].devicefirmware.image, env['image1b'])
        # upgrade all related
        env['build2'].upgrade_related_devices()
        # ensure image is changed
        env['d1'].devicefirmware.refresh_from_db()
        env['d2'].devicefirmware.refresh_from_db()
        self.assertEqual(env['d1'].devicefirmware.image, env['image2a'])
        self.assertEqual(env['d2'].devicefirmware.image, env['image2b'])
        # ensure upgrade operation objects have been created
        self.assertEqual(UpgradeOperation.objects.count(), 2)

    def test_upgrade_firmwareless_devices(self):
        env = self._create_upgrade_env(device_firmware=False)
        # check everything is as expected
        self.assertEqual(UpgradeOperation.objects.count(), 0)
        self.assertFalse(hasattr(env['d1'], 'devicefirmware'))
        self.assertFalse(hasattr(env['d2'], 'devicefirmware'))
        # upgrade all related
        env['build2'].upgrade_firmwareless_devices()
        env['d1'].refresh_from_db()
        env['d2'].refresh_from_db()
        self.assertEqual(env['d1'].devicefirmware.image, env['image2a'])
        self.assertEqual(env['d2'].devicefirmware.image, env['image2b'])
        # ensure upgrade operation objects have been created
        self.assertEqual(UpgradeOperation.objects.count(), 2)
