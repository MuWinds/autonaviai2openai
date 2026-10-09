"""
aos_sign.py -- Offline reproduction of the AMap (高德) AOS request signature.

Reverse-engineered from 高德 v17.00 (verified against the device's native
libserverkey.so: 12/12 inputs matched, incl. empty/long/unicode):

    sign(input) = MD5_HEX_UPPERCASE(input)
    input       = channel + <other signKey values concatenated, no separator>
                  + "@" + AOS_KEY

where:
  * `channel` is always moved to the front of the signKey list,
  * the `_aosmd5` key is removed before signing,
  * AOS_KEY is a constant embedded in libserverkey.so.

So the whole "native signature" is just MD5 — no device or Frida needed.
"""
import hashlib

# Constant embedded in libserverkey.so (also visible as a .rodata string).
AOS_KEY = "xnaEwInMxaMQ2m0cw6Y1bDm7ns0YVxYS9v7JlC8I"
AOS_CHANNEL = "amap7a"


def aos_sign_raw(s: str) -> str:
    """MD5 hex (uppercase) of the raw input string."""
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def aos_sign_values(sign_keys, values, channel=AOS_CHANNEL, aos_key=AOS_KEY) -> str:
    """
    Build the AOS signature from an ordered signKey list and a value lookup.

    sign_keys : iterable of key names (order as registered by the caller)
    values    : dict-like, values.get(key, "") -> str
    """
    keys = [k for k in sign_keys if k != "_aosmd5"]
    if "channel" in keys:
        keys.remove("channel")
        keys.insert(0, "channel")
    else:
        keys.insert(0, "channel")

    sb = []
    for k in keys:
        v = values.get(k, "") if hasattr(values, "get") else ""
        sb.append("" if v is None else str(v))
    payload = "".join(sb) + "@" + aos_key
    return aos_sign_raw(payload)


def aos_sign_channel_only(channel=AOS_CHANNEL, aos_key=AOS_KEY) -> str:
    """sign for the common case signKeys={adiu, channel} with adiu empty."""
    return aos_sign_raw(channel + "@" + aos_key)


if __name__ == "__main__":
    # self-check against captured device values
    checks = [
        ("", "D41D8CD98F00B204E9800998ECF8427E"),
        ("channel=demo@x", "8BD0BF0A55AE68576D84B7A84041FED7"),
        (AOS_CHANNEL + "@" + AOS_KEY, "422942E485BC93857384D612081099F3"),
    ]
    for inp, want in checks:
        got = aos_sign_raw(inp)
        print("OK " if got == want else "XX ", repr(inp[:40]), got)
    print("signKeys={adiu,channel} adiu='' ->", aos_sign_channel_only())
