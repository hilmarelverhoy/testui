import os
from django.test import SimpleTestCase


class Crash(SimpleTestCase):
    def test_hard_exit(self):
        os._exit(3)
