import os
import unittest
import shutil
import tempfile
from pycoin.serialize import b2h
from picopayments import ctrl
from picopayments import cli
from picopayments import db


class TestDB(unittest.TestCase):

    def setUp(self):
        self.basedir = tempfile.mkdtemp(prefix="picopayments_test_")
        # TODO start mock counterparty service
        ctrl.initialize(cli.parse([
            "--testnet",
            "--basedir={0}".format(self.basedir)
        ]))

    def tearDown(self):
        shutil.rmtree(self.basedir)

    def test_handle_exists(self):

        def _create_connection(handle):
            db.add_hub_connection({
                "asset": b2h(os.urandom(32)),
                "setup_ttl": b2h(os.urandom(32)),
                "deposit_limit": b2h(os.urandom(32)),
                "deposit_ratio": b2h(os.urandom(32)),
                "timeout_limit": b2h(os.urandom(32)),
                "sync_fee": b2h(os.urandom(32)),
                "hub_wif": b2h(os.urandom(32)),
                "hub_pubkey": b2h(os.urandom(32)),
                "hub_address": b2h(os.urandom(32)),
                "client_pubkey": b2h(os.urandom(32)),
                "client_address": b2h(os.urandom(32)),
                "secret_hash": b2h(os.urandom(32)),
                "secret_value": b2h(os.urandom(32)),
                "hub2client_spend_secret_hash": b2h(os.urandom(32)),
                "handle": handle,
                "hub_rpc_url": b2h(os.urandom(32)),
            })

        _create_connection("a")
        _create_connection("b")

        self.assertTrue(db.handles_exist(["a"]))  # test single
        self.assertTrue(db.handles_exist(["b"]))  # test single
        self.assertTrue(db.handles_exist(["a", "b"]))  # test multiple
        self.assertFalse(db.handles_exist(["c"]))  # test missing single
        self.assertFalse(db.handles_exist(["a", "c"]))  # test one missing
        self.assertFalse(db.handles_exist(["c", "d"]))  # test all missing


if __name__ == "__main__":
    unittest.main()