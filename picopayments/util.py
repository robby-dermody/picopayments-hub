import pycoin


def wif2sec(wif):
    return pycoin.key.Key.from_text(wif).sec()


def wif2address(wif):
    return pycoin.key.Key.from_text(wif).address()


def wif2secretexponent(wif):
    return pycoin.key.Key.from_text(wif).secret_exponent()


def sec2address(sec, netcode="BTC"):
    prefix = pycoin.networks.address_prefix_for_netcode(netcode)
    digest = pycoin.encoding.hash160(sec)
    return pycoin.encoding.hash160_sec_to_bitcoin_address(digest, prefix)


def script2address(script, netcode="BTC"):
    return pycoin.tx.pay_to.address_for_pay_to_script(script, netcode=netcode)
