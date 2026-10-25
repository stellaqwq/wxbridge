import pytest

from wxbridge.onebot.protocol import ActionError, boolean, escape, number, text_message


def test_cq_text_roundtrip_and_escaping_order():
    original = "[CQ:image,file=a.png] & &#91;你好"
    assert text_message(escape(original)) == original
    assert text_message(original, auto_escape=True) == original
    assert text_message({"type": "text", "data": {"text": original}}) == original
    assert (
        text_message(
            [
                {"type": "text", "data": {"text": "第一段"}},
                {"type": "text", "data": {"text": "\n第二段"}},
            ]
        )
        == "第一段\n第二段"
    )


@pytest.mark.parametrize(
    "message",
    [
        "[CQ:image,file=x]",
        "之前[CQ:reply,id=1]之后",
        [{"type": "image", "data": {"file": "x"}}],
        [{"type": "text", "data": {"text": "不要部分发送"}}, {"type": "record", "data": {}}],
        None,
        [],
        [None],
        {"type": "text", "data": {}},
        "",
        "\n",
        "长" * 4001,
    ],
)
def test_invalid_and_unsupported_messages_fail_before_send(message):
    with pytest.raises(ActionError):
        text_message(message)


@pytest.mark.parametrize("value", [True, False, 1.2, -1, 0, "1.2", "-1", "", {}, 2**63])
def test_id_is_integral_positive_and_int64(value):
    with pytest.raises(ActionError):
        number(value, "user_id")


def test_http_string_coercion():
    assert number("10001", "user_id") == 10001
    assert boolean("false") is False
    assert boolean("yes") is True
    with pytest.raises(ActionError):
        boolean("maybe")
