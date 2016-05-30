# coding: utf-8
# Copyright (c) 2016 Fabian Barkhau <fabian.barkhau@gmail.com>
# License: MIT (see LICENSE file)


import os
import copy
import pycoin
import time
import json
import requests
from threading import RLock
from btctxstore import BtcTxStore
from requests.auth import HTTPBasicAuth
from bitcoinrpc.authproxy import AuthServiceProxy
from picopayments import util
from picopayments import validate
from picopayments.scripts import get_commit_revoke_secret_hash
from picopayments.scripts import get_deposit_spend_secret_hash
from picopayments.scripts import get_deposit_payee_pubkey
from picopayments.scripts import get_commit_spend_secret_hash
from picopayments.scripts import get_commit_payee_pubkey
from picopayments.scripts import get_commit_delay_time
from picopayments import exceptions
from picopayments import scripts
from picopayments.scripts import get_deposit_expire_time
from picopayments.scripts import get_deposit_payer_pubkey
from picopayments.scripts import compile_commit_script
from picopayments.scripts import compile_deposit_script
from picopayments.scripts import DepositScriptHandler
from picopayments.scripts import CommitScriptHandler


# FIXME fees per kb, auto adjust to market price or get from counterparty
DEFAULT_TXFEE = 10000  # FIXME dont hardcode tx fee
DEFAULT_DUSTSIZE = 5430  # FIXME dont hardcode dust size
DEFAULT_TESTNET = False
DEFAULT_COUNTERPARTY_RPC_MAINNET_URL = "http://public.coindaddy.io:4000/api/"
DEFAULT_COUNTERPARTY_RPC_TESTNET_URL = "http://public.coindaddy.io:14000/api/"
DEFAULT_COUNTERPARTY_RPC_USER = "rpc"
DEFAULT_COUNTERPARTY_RPC_PASSWORD = "1234"


class Channel(object):

    state = {
        "payer_wif": None,
        "payee_wif": None,
        "spend_secret": None,
        "deposit_script": None,
        "deposit_rawtx": None,
        "expire_rawtxs": [],  # ["rawtx", ...]
        "change_rawtxs": [],  # ["rawtx", ...]
        "revoke_rawtxs": [],  # ["rawtx", ...]
        "payout_rawtxs": [],  # ["rawtx", ...]

        # Quantity not needed as payer may change it. If its heigher its
        # against our self intrest to throw away money. If its lower it
        # gives us a better resolution when reversing the channel.
        "commits_requested": [],  # ["revoke_secret_hex"]

        # must be ordered lowest to heighest at all times!
        "commits_active": [],     # [{
        #                             "rawtx": hex,
        #                             "script": hex,
        #                             "revoke_secret": hex
        #                         }]

        "commits_revoked": [],    # [{
        #                            "rawtx": hex,  # unneeded?
        #                            "script": hex,
        #                            "revoke_secret": hex
        #                         }]
    }

    def __init__(self, asset, user=DEFAULT_COUNTERPARTY_RPC_USER,
                 password=DEFAULT_COUNTERPARTY_RPC_PASSWORD,
                 api_url=None, testnet=DEFAULT_TESTNET, dryrun=False,
                 fee=DEFAULT_TXFEE, dust_size=DEFAULT_DUSTSIZE):
        """Initialize payment channel controler.

        Args:
            asset (str): Counterparty asset name.
            user (str): Counterparty API username.
            password (str): Counterparty API password.
            api_url (str): Counterparty API url.
            testnet (bool): True if running on testnet, otherwise mainnet.
            dryrun (bool): If True nothing will be published to the blockchain.
            fee (int): The transaction fee to use.
            dust_size (int): The default dust size for counterparty outputs.
        """

        if testnet:
            default_url = DEFAULT_COUNTERPARTY_RPC_TESTNET_URL
        else:
            default_url = DEFAULT_COUNTERPARTY_RPC_MAINNET_URL

        self.dryrun = dryrun
        self.fee = fee
        self.dust_size = dust_size
        self.api_url = api_url or default_url
        self.testnet = testnet
        self.user = user
        self.password = password
        self.asset = asset
        self.netcode = "BTC" if not self.testnet else "XTN"
        self.btctxstore = BtcTxStore(testnet=self.testnet, dryrun=dryrun,
                                     service="insight")
        self.bitcoind_rpc = AuthServiceProxy(  # XXX to publish
            "http://bitcoinrpcuser:bitcoinrpcpass@127.0.0.1:18332"
        )
        self.mutex = RLock()

    def save(self):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self._order_active(self.state)
            return copy.deepcopy(self.state)

    def load(self, state):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self.state = copy.deepcopy(state)
            return self

    def clear(self):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self.state = {
                "payer_wif": None,
                "payee_wif": None,
                "spend_secret": None,
                "deposit_script": None,
                "deposit_rawtx": None,
                "expire_rawtxs": [],
                "change_rawtxs": [],
                "payout_rawtxs": [],
                "revoke_rawtxs": [],
                "commits_requested": [],
                "commits_active": [],
                "commits_revoked": []
            }

    def setup(self, payee_wif):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self.clear()
            self.state["payee_wif"] = payee_wif
            payee_pubkey = util.wif2pubkey(self.state["payee_wif"])
            secret = os.urandom(32)  # secure random number
            self.state["spend_secret"] = util.b2h(secret)
            spend_secret_hash = util.b2h(util.hash160(secret))
            return payee_pubkey, spend_secret_hash

    def set_deposit(self, rawtx, script_hex):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self._validate_payer_deposit(rawtx, script_hex)

            script = util.h2b(script_hex)
            self._validate_deposit_spend_secret_hash(self.state, script)
            self._validate_deposit_payee_pubkey(self.state, script)
            self.state["deposit_rawtx"] = rawtx
            self.state["deposit_script"] = script_hex

    def request_commit(self, quantity):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self._validate_transfer_quantity(self.state, quantity)
            secret = util.b2h(os.urandom(32))  # secure random number
            secret_hash = util.hash160hex(secret)
            self.state["commits_requested"].append(secret)
            return quantity, secret_hash

    def set_commit(self, rawtx, script_hex):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self._validate_payer_commit(rawtx, script_hex)

            script = util.h2b(script_hex)
            self._validate_commit_secret_hash(self.state, script)
            self._validate_commit_payee_pubkey(self.state, script)

            revoke_secret_hash = get_commit_revoke_secret_hash(script)
            for revoke_secret in self.state["commits_requested"][:]:

                # revoke secret hash must match as it would
                # otherwise break the channels reversability
                if revoke_secret_hash == util.hash160hex(revoke_secret):

                    # remove from requests
                    self.state["commits_requested"].remove(revoke_secret)

                    # add to active
                    self._order_active(self.state)
                    self.state["commits_active"].append({
                        "rawtx": rawtx, "script": script_hex,
                        "revoke_secret": revoke_secret
                    })
                    return self.get_transferred_amount()

            return None

    def revoke_until(self, quantity):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            secrets = []
            self._order_active(self.state)
            for commit in reversed(self.state["commits_active"][:]):
                if quantity < self._get_quantity(commit["rawtx"]):
                    secrets.append(commit["revoke_secret"])
                else:
                    break
            self.revoke_all(secrets)
            return secrets

    def close_channel(self):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            assert(len(self.state["commits_active"]) > 0)
            self._order_active(self.state)
            commit = self.state["commits_active"][-1]
            rawtx = self._finalize_commit(
                self.state["payee_wif"], commit["rawtx"],
                util.h2b(self.state["deposit_script"])
            )
            commit["rawtx"] = rawtx  # update commit
            return util.gettxid(rawtx)

    def payee_update(self):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:

            # payout recoverable commits
            scripts = self._get_payout_recoverable(self.state)
            if len(scripts) > 0:
                for script in scripts:
                    rawtx = self._recover_commit(
                        self.state["payee_wif"], script, None,
                        self.state["spend_secret"], "payout"
                    )
                    self.state["payout_rawtxs"].append(rawtx)

    def revoke_all(self, secrets):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            return list(map(lambda s: self._revoke(self.state, s), secrets))

    def is_deposit_confirmed(self, minconfirms=1):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            validate.unsigned(minconfirms)
            script = util.h2b(self.state["deposit_script"])
            address = util.script2address(script, self.netcode)
            if self._get_address_balance(address) == (0, 0):
                return False
            rawtx = self.state["deposit_rawtx"]
            confirms = self.btctxstore.confirms(util.gettxid(rawtx)) or 0
            return confirms >= minconfirms

    def get_transferred_amount(self):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            if len(self.state["commits_active"]) == 0:
                return 0
            self._order_active(self.state)
            commit = self.state["commits_active"][-1]
            return self._get_quantity(commit["rawtx"])

    def create_commit(self, quantity, revoke_secret_hash, delay_time):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            self._validate_transfer_quantity(self.state, quantity)
            rawtx, script = self._create_commit(
                self.state["payer_wif"], util.h2b(self.state["deposit_script"]),
                quantity, revoke_secret_hash, delay_time
            )
            script_hex = util.b2h(script)
            self._order_active(self.state)
            self.state["commits_active"].append({
                "rawtx": rawtx, "script": script_hex, "revoke_secret": None
            })
            return {"rawtx": rawtx, "script": script_hex}

    def deposit(self, payer_wif, payee_pubkey, spend_secret_hash,
                expire_time, quantity):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state

        with self.mutex:
            self._validate_deposit(payer_wif, payee_pubkey, spend_secret_hash,
                                   expire_time, quantity)

            self.clear()
            self.state["payer_wif"] = payer_wif
            rawtx, script = self._deposit(
                self.state["payer_wif"], payee_pubkey,
                spend_secret_hash, expire_time, quantity
            )
            self.state["deposit_rawtx"] = rawtx
            self.state["deposit_script"] = util.b2h(script)
            return {"rawtx": rawtx, "script": util.b2h(script)}

    def payer_update(self):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:

            # If revoked commit published, recover funds asap!
            revokable = self._get_revoke_recoverable(self.state)
            if len(revokable) > 0:
                for script, secret in revokable:
                    rawtx = self._recover_commit(
                        self.state["payer_wif"], script, secret, None, "revoke")
                    self.state["revoke_rawtxs"].append(rawtx)

            # If spend secret exposed by payout, recover change!
            script = util.h2b(self.state["deposit_script"])
            address = util.script2address(script, self.netcode)
            if self._can_spend_from_address(address):
                spend_secret = self._find_spend_secret(self.state)
                if spend_secret is not None:
                    self.state = self._change_recover(self.state, spend_secret)

            # If deposit expired recover the coins!
            if self._can_expire_recover(self.state):
                script = util.h2b(self.state["deposit_script"])
                rawtx = self._recover_deposit(self.state["payer_wif"],
                                              script, "expire", None)
                self.state["expire_rawtxs"].append(rawtx)

    def payout_confirmed(self, minconfirms=1):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            validate.unsigned(minconfirms)
            return self._all_confirmed(self.state["payout_rawtxs"],
                                       minconfirms=minconfirms)

    def change_confirmed(self, minconfirms=1):
        # FIXME add doc string
        # FIXME validate all input
        # FIXME validate state
        with self.mutex:
            validate.unsigned(minconfirms)
            return self._all_confirmed(self.state["change_rawtxs"],
                                       minconfirms=minconfirms)

    def _rpc_call(self, payload):
        headers = {'content-type': 'application/json'}
        auth = HTTPBasicAuth(self.user, self.password)
        response = requests.post(self.api_url, data=json.dumps(payload),
                                 headers=headers, auth=auth)
        response_data = json.loads(response.text)
        if "result" not in response_data:
            raise Exception("Counterparty rpc call failed! {0}".format(
                repr(response.text)
            ))
        return response_data["result"]

    def _valid_channel_unused(self, channel_address):
        txs = self.btctxstore.get_transactions(channel_address)
        if len(txs) > 0:
            raise exceptions.ChannelAlreadyUsed(channel_address, txs)

    def _recover_tx(self, dest_address, script, sequence=None):

        # get channel info
        src_address = util.script2address(script, self.netcode)
        asset_balance, btc_balance = self._get_address_balance(src_address)

        # create expire tx
        rawtx = self._create_tx(src_address, dest_address, asset_balance,
                                extra_btc=btc_balance - self.fee)

        # prep for script compliance and signing
        tx = pycoin.tx.Tx.from_hex(rawtx)
        if sequence:
            tx.version = 2  # enable relative lock-time, see bip68 & bip112
        for txin in tx.txs_in:
            if sequence:
                txin.sequence = sequence  # relative lock-time
            utxo_tx = self.btctxstore.service.get_tx(txin.previous_hash)
            tx.unspents.append(utxo_tx.txs_out[txin.previous_index])

        return tx

    def _recover_commit(self, wif, script, revoke_secret,
                        spend_secret, spend_type):

        dest_address = util.wif2address(wif)
        delay_time = get_commit_delay_time(script)
        tx = self._recover_tx(dest_address, script, delay_time)

        # sign
        hash160_lookup = pycoin.tx.pay_to.build_hash160_lookup(
            [util.wif2secretexponent(wif)]
        )
        p2sh_lookup = pycoin.tx.pay_to.build_p2sh_lookup([script])
        with CommitScriptHandler(delay_time):
            tx.sign(hash160_lookup, p2sh_lookup=p2sh_lookup,
                    spend_type=spend_type, spend_secret=spend_secret,
                    revoke_secret=revoke_secret)

        rawtx = tx.as_hex()
        assert(self._transaction_complete(rawtx))
        self._publish(rawtx)
        return rawtx

    def _recover_deposit(self, wif, script, spend_type, spend_secret):

        dest_address = util.wif2address(wif)
        expire_time = get_deposit_expire_time(script)
        tx = self._recover_tx(dest_address, script,
                              expire_time if spend_type == "expire" else None)

        # sign
        hash160_lookup = pycoin.tx.pay_to.build_hash160_lookup(
            [util.wif2secretexponent(wif)]
        )
        p2sh_lookup = pycoin.tx.pay_to.build_p2sh_lookup([script])
        with DepositScriptHandler(expire_time):
            tx.sign(hash160_lookup, p2sh_lookup=p2sh_lookup,
                    spend_type=spend_type, spend_secret=spend_secret)

        rawtx = tx.as_hex()
        assert(self._transaction_complete(rawtx))
        self._publish(rawtx)
        return rawtx

    def _finalize_commit(self, payee_wif, commit_rawtx, deposit_script):

        # prep for signing
        tx = pycoin.tx.Tx.from_hex(commit_rawtx)
        for txin in tx.txs_in:
            utxo_tx = self.btctxstore.service.get_tx(txin.previous_hash)
            tx.unspents.append(utxo_tx.txs_out[txin.previous_index])

        # sign tx
        hash160_lookup = pycoin.tx.pay_to.build_hash160_lookup(
            [util.wif2secretexponent(payee_wif)]
        )
        p2sh_lookup = pycoin.tx.pay_to.build_p2sh_lookup([deposit_script])
        expire_time = get_deposit_expire_time(deposit_script)
        with DepositScriptHandler(expire_time):
            tx.sign(hash160_lookup, p2sh_lookup=p2sh_lookup,
                    spend_type="finalize_commit", spend_secret=None)

        rawtx = tx.as_hex()
        self._publish(rawtx)
        return rawtx

    def _create_commit(self, payer_wif, deposit_script, quantity,
                       revoke_secret_hash, delay_time):

        # create script
        payer_pubkey = get_deposit_payer_pubkey(deposit_script)
        assert(util.wif2pubkey(payer_wif) == payer_pubkey)
        payee_pubkey = get_deposit_payee_pubkey(deposit_script)
        spend_secret_hash = get_deposit_spend_secret_hash(deposit_script)
        commit_script = compile_commit_script(
            payer_pubkey, payee_pubkey, spend_secret_hash,
            revoke_secret_hash, delay_time
        )

        # create tx
        src_address = util.script2address(deposit_script, self.netcode)
        dest_address = util.script2address(commit_script, self.netcode)
        asset_balance, btc_balance = self._get_address_balance(src_address)
        if quantity == asset_balance:  # spend all btc as change tx not needed
            extra_btc = btc_balance - self.fee
        else:  # provide extra btc for future payout/revoke tx fees
            extra_btc = (self.fee + self.dust_size)
        rawtx = self._create_tx(src_address, dest_address,
                                quantity, extra_btc=extra_btc)

        # prep for signing
        tx = pycoin.tx.Tx.from_hex(rawtx)
        for txin in tx.txs_in:
            utxo_tx = self.btctxstore.service.get_tx(txin.previous_hash)
            tx.unspents.append(utxo_tx.txs_out[txin.previous_index])

        # sign tx
        hash160_lookup = pycoin.tx.pay_to.build_hash160_lookup(
            [util.wif2secretexponent(payer_wif)]
        )
        p2sh_lookup = pycoin.tx.pay_to.build_p2sh_lookup([deposit_script])
        expire_time = get_deposit_expire_time(deposit_script)
        with DepositScriptHandler(expire_time):
            tx.sign(hash160_lookup, p2sh_lookup=p2sh_lookup,
                    spend_type="create_commit", spend_secret=None)

        return tx.as_hex(), commit_script

    def _deposit(self, payer_wif, payee_pubkey, spend_secret_hash,
                 expire_time, quantity):

        payer_pubkey = util.wif2pubkey(payer_wif)
        script = compile_deposit_script(payer_pubkey, payee_pubkey,
                                        spend_secret_hash, expire_time)
        dest_address = util.script2address(script, self.netcode)
        self._valid_channel_unused(dest_address)
        payer_address = util.wif2address(payer_wif)

        # provide extra btc for future closing channel fees
        # change tx or recover + commit tx + payout tx or revoke tx
        extra_btc = (self.fee + self.dust_size) * 3

        rawtx = self._create_tx(payer_address, dest_address,
                                quantity, extra_btc=extra_btc)
        rawtx = self.btctxstore.sign_tx(rawtx, [payer_wif])
        self._publish(rawtx)
        return rawtx, script

    def _create_tx(self, source_address, dest_address, quantity, extra_btc=0):
        assert(extra_btc >= 0)
        rawtx = self._rpc_call({
            "method": "create_send",
            "params": {
                "source": source_address,
                "destination": dest_address,
                "quantity": quantity,
                "asset": self.asset,
                "regular_dust_size": extra_btc or self.dust_size,
                "fee": self.fee
            },
            "jsonrpc": "2.0",
            "id": 0,
        })
        assert(self._get_quantity(rawtx) == quantity)
        return rawtx

    def _can_spend_from_address(self, address):

        # has assets, btc
        if self._get_address_balance(address) == (0, 0):
            return False

        # TODO check if btc > fee

        # can only spend if all txs confirmed
        txids = self.btctxstore.get_transactions(address)
        latest_confirms = self.btctxstore.confirms(txids[0])
        return latest_confirms > 0

    def _get_address_balance(self, address):
        result = self._rpc_call({
            "method": "get_balances",
            "params": {
                "filters": [
                    {'field': 'address', 'op': '==', 'value': address},
                    {'field': 'asset', 'op': '==', 'value': self.asset},
                ]
            },
            "jsonrpc": "2.0",
            "id": 0,
        })
        if not result:  # FIXME what causes this?
            return 0, 0
        asset_balance = result[0]["quantity"]
        utxos = self.btctxstore.retrieve_utxos([address])
        btc_balance = sum(map(lambda utxo: utxo["value"], utxos))
        return asset_balance, btc_balance

    def _publish(self, rawtx):
        txid = util.gettxid(rawtx)
        if self.dryrun:
            return txid
        while self.btctxstore.confirms(util.gettxid(rawtx)) is None:
            try:
                self.bitcoind_rpc.sendrawtransaction(rawtx)
                return util.gettxid(rawtx)
                # see http://counterparty.io/docs/api/#wallet-integration
            except Exception as e:
                print("publishing failed: {0} {1}".format(type(e), e))
            time.sleep(10)

    def _get_quantity(self, rawtx):
        result = self._rpc_call({
            "method": "get_tx_info",
            "params": {
                "tx_hex": rawtx
            },
            "jsonrpc": "2.0",
            "id": 0,
        })
        src, dest, btc, fee, data = result
        result = self._rpc_call({
            "method": "unpack",
            "params": {
                "data_hex": data
            },
            "jsonrpc": "2.0",
            "id": 0,
        })
        message_type_id, unpacked = result
        if message_type_id != 0:
            msg = "Incorrect message type id: {0} != {1}"
            raise ValueError(msg.format(message_type_id, 0))
        if self.asset != unpacked["asset"]:
            msg = "Incorrect asset: {0} != {1}"
            raise ValueError(msg.format(self.asset, unpacked["asset"]))
        return unpacked["quantity"]

    def _transaction_complete(self, rawtx):
        tx = pycoin.tx.Tx.from_hex(rawtx)
        for txin in tx.txs_in:
            utxo_tx = self.btctxstore.service.get_tx(txin.previous_hash)
            tx.unspents.append(utxo_tx.txs_out[txin.previous_index])
        return tx.bad_signature_count() == 0

    def _validate_transfer_quantity(self, state, quantity):
        transferred = self.get_transferred_amount()
        if quantity <= transferred:
            msg = "Amount not greater transferred: {0} <= {1}"
            raise ValueError(msg.format(quantity, transferred))

        total = self._get_quantity(state["deposit_rawtx"])
        if quantity > total:
            msg = "Amount greater total: {0} > {1}"
            raise ValueError(msg.fromat(quantity, total))

    def _order_active(self, state):

        def sort_func(entry):
            return self._get_quantity(entry["rawtx"])
        state["commits_active"].sort(key=sort_func)

    def _revoke(self, state, secret):
        with self.mutex:
            secret_hash = util.hash160hex(secret)
            for commit in state["commits_active"][:]:
                script = util.h2b(commit["script"])
                if secret_hash == get_commit_revoke_secret_hash(script):
                    state["commits_active"].remove(commit)
                    commit["revoke_secret"] = secret  # save secret
                    state["commits_revoked"].append(commit)
                    return copy.deepcopy(commit)
            return None

    def _all_confirmed(self, rawtxs, minconfirms=1):
        validate.unsigned(minconfirms)
        if len(rawtxs) == 0:
            return False
        for rawtx in rawtxs:
            confirms = self.btctxstore.confirms(util.gettxid(rawtx)) or 0
            if confirms < minconfirms:
                return False
        return True

    def _commit_spent(self, state, commit):
        txid = util.gettxid(commit["rawtx"])
        for rawtx in (state["payout_rawtxs"] + state["revoke_rawtxs"] +
                      state["change_rawtxs"] + state["expire_rawtxs"]):
            tx = pycoin.tx.Tx.from_hex(rawtx)
            for txin in tx.txs_in:
                if util.b2h_rev(txin.previous_hash) == txid:
                    return True
        return False

    def _validate_deposit_spend_secret_hash(self, state, script):
        given_spend_secret_hash = get_deposit_spend_secret_hash(script)
        own_spend_secret_hash = util.hash160hex(state["spend_secret"])
        if given_spend_secret_hash != own_spend_secret_hash:
            msg = "Incorrect spend secret hash: {0} != {1}"
            raise ValueError(msg.format(
                given_spend_secret_hash, own_spend_secret_hash
            ))

    def _validate_deposit_payee_pubkey(self, state, script):
        given_payee_pubkey = get_deposit_payee_pubkey(script)
        own_payee_pubkey = util.wif2pubkey(state["payee_wif"])
        if given_payee_pubkey != own_payee_pubkey:
            msg = "Incorrect payee pubkey: {0} != {1}"
            raise ValueError(msg.format(
                given_payee_pubkey, own_payee_pubkey
            ))

    def _validate_payer_deposit(self, rawtx, script_hex):
        tx = pycoin.tx.Tx.from_hex(rawtx)
        assert(tx.bad_signature_count() == 1)

        # TODO validate script
        # TODO check given script and rawtx match
        # TODO check given script is deposit script

    def _validate_payer_commit(self, rawtx, script_hex):
        tx = pycoin.tx.Tx.from_hex(rawtx)
        assert(tx.bad_signature_count() == 1)

        # TODO validate script
        # TODO validate rawtx signed by payer
        # TODO check it is for the current deposit
        # TODO check given script and rawtx match
        # TODO check given script is commit script

    def _validate_commit_secret_hash(self, state, script):
        given_spend_secret_hash = get_commit_spend_secret_hash(script)
        own_spend_secret_hash = util.hash160hex(state["spend_secret"])
        if given_spend_secret_hash != own_spend_secret_hash:
            msg = "Incorrect spend secret hash: {0} != {1}"
            raise ValueError(msg.format(
                given_spend_secret_hash, own_spend_secret_hash
            ))

    def _validate_commit_payee_pubkey(self, state, script):
        given_payee_pubkey = get_commit_payee_pubkey(script)
        own_payee_pubkey = util.wif2pubkey(state["payee_wif"])
        if given_payee_pubkey != own_payee_pubkey:
            msg = "Incorrect payee pubkey: {0} != {1}"
            raise ValueError(msg.format(
                given_payee_pubkey, own_payee_pubkey
            ))

    def _get_payout_recoverable(self, state):
        scripts = []
        for commit in (state["commits_active"] +
                       state["commits_revoked"]):
            script = util.h2b(commit["script"])
            delay_time = get_commit_delay_time(script)
            address = util.script2address(
                script, netcode=self.netcode
            )
            if self._commit_spent(state, commit):
                continue
            if self._can_spend_from_address(address):
                utxos = self.btctxstore.retrieve_utxos([address])
                for utxo in utxos:
                    txid = utxo["txid"]
                    confirms = self.btctxstore.confirms(txid)
                    if confirms >= delay_time:
                        scripts.append(script)
        return scripts

    def _can_expire_recover(self, state):
        return (
            # we know the payer wif
            state["payer_wif"] is not None and

            # deposit was made
            state["deposit_rawtx"] is not None and
            state["deposit_script"] is not None and

            # deposit expired
            self._is_deposit_expired(state) and

            # funds to recover
            self._can_deposit_spend(state)
        )

    def _can_deposit_spend(self, state):
        script = util.h2b(state["deposit_script"])
        address = util.script2address(script, self.netcode)
        return self._can_spend_from_address(address)

    def _is_deposit_expired(self, state):
        script = util.h2b(state["deposit_script"])
        t = get_deposit_expire_time(script)
        rawtx = state["deposit_rawtx"]
        confirms = self.btctxstore.confirms(util.gettxid(rawtx)) or 0
        return confirms >= t

    def _validate_deposit(self, payer_wif, payee_pubkey, spend_secret_hash,
                          expire_time, quantity):

        # validate untrusted input data
        validate.wif(payer_wif, self.netcode)
        validate.pubkey(payee_pubkey)
        validate.hash160(spend_secret_hash)
        validate.sequence(expire_time)
        validate.quantity(quantity)

        # get balances
        address = util.wif2address(payer_wif)
        asset_balance, btc_balance = self._get_address_balance(address)

        # check asset balance
        if asset_balance < quantity:
            raise exceptions.InsufficientFunds(quantity, asset_balance)

        # check btc balance
        extra_btc = (self.fee + self.dust_size) * 3
        if btc_balance < extra_btc:
            raise exceptions.InsufficientFunds(extra_btc, btc_balance)

    def _find_spend_secret(self, state):
        for commit in state["commits_active"] + \
                state["commits_revoked"]:
            script = util.h2b(commit["script"])
            address = util.script2address(
                script, netcode=self.netcode
            )
            txs = self.btctxstore.get_transactions(address)
            if len(txs) == 1:
                continue  # only the commit, no payout
            for txid in txs:
                rawtx = self.btctxstore.retrieve_tx(txid)
                spend_secret = scripts.get_spend_secret(rawtx, script)
                if spend_secret is not None:
                    return spend_secret
        return None

    def _change_recover(self, state, spend_secret):
        script = util.h2b(state["deposit_script"])
        rawtx = self._recover_deposit(state["payer_wif"], script,
                                      "change", spend_secret)
        state["change_rawtxs"].append(rawtx)
        return state

    def _get_revoke_recoverable(self, state):
        commits_revoked = state["commits_revoked"]
        revokable = []  # (secret, script)
        for commit in commits_revoked:
            script = util.h2b(commit["script"])
            address = util.script2address(
                script, netcode=self.netcode
            )
            if self._can_spend_from_address(address):
                revokable.append((script, commit["revoke_secret"]))
        return revokable