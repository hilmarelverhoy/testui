import os
import sys
import unittest
from unittest import mock

from django.test import TestCase


def helper(x):
    assert x == 2, "helper wanted 2"


class Basic(TestCase):
    def test_pass(self):
        self.assertTrue(True)

    def test_fail(self):
        a = 1
        self.assertEqual(a, 2)

    def test_assert_in_helper(self):
        helper(3)

    def test_error(self):
        {}["missing"]

    @unittest.skip("not today")
    def test_skip(self):
        pass

    def test_subtests(self):
        for i in range(2):
            with self.subTest(i=i):
                self.assertEqual(i, 0)


class Hostile(TestCase):
    def test_prints_junk(self):
        sys.stdout.write("\x00\xff garbage \n" * 1000)
        sys.stderr.write("noise\n")

    def test_mocks_everything(self):
        with mock.patch("socket.socket"), mock.patch("time.monotonic", return_value=0), \
                mock.patch("json.dumps", side_effect=RuntimeError), mock.patch("builtins.open"):
            pass

    def test_chdir(self):
        os.chdir("/")
        self.assertEqual(1, 2)

    def test_huge_message(self):
        self.fail("x" * 5_000_000)
