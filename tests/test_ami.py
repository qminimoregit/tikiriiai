from app.telephony import ami


class FakeTransport:
    menu_requested = False
    closed = False


def test_parse_event():
    ev = ami.parse_event("Event: DTMFEnd\r\nChannel: PJSIP/ucmtrunk-0000002a\r\nDigit: #\r\nDirection: Received")
    assert ev["Event"] == "DTMFEnd" and ev["Digit"] == "#" and ev["Channel"] == "PJSIP/ucmtrunk-0000002a"


def test_only_a_received_hash_on_a_tracked_call_returns_to_the_menu():
    ami.track("PJSIP/a-1", FakeTransport(), "from-ucm-trunk", "7001")
    try:
        base = {"Event": "DTMFEnd", "Channel": "PJSIP/a-1", "Digit": "#", "Direction": "Received"}
        assert ami.menu_request(base) == "PJSIP/a-1"
        assert ami.menu_request({**base, "Digit": "5"}) is None
        assert ami.menu_request({**base, "Direction": "Sent"}) is None
        assert ami.menu_request({**base, "Channel": "PJSIP/other-2"}) is None
        assert ami.menu_request({**base, "Event": "DTMFBegin"}) is None
    finally:
        ami.untrack("PJSIP/a-1")
    assert ami.menu_request({"Event": "DTMFEnd", "Channel": "PJSIP/a-1", "Digit": "#", "Direction": "Received"}) is None
