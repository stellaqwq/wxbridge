from dataclasses import replace
from pathlib import Path

import pytest

from wxbridge import BatchSendError, ChatChanged, DeliveryUncertain, Message, WeChat, WeChatError
from wxbridge.media import identity_content, local_attachment, message_details, safe_filename
from wxbridge.models import Snapshot


class MediaBackend:
    def __init__(self):
        self.state = Snapshot("测试好友", "friend", (), "ctx")
        self.prepared = []
        self.submits = 0
        self.echo = True
        self.wrong_name = False
        self.prepare_error = False
        self.submit_error = False

    def snapshot(self):
        return self.state

    def prepare_attachment(self, path, who, kind, file_state):
        if self.prepare_error:
            raise WeChatError("已有附件草稿")
        self.prepared.append((path, who, kind, file_state))

    def submit_attachment(self, who):
        self.submits += 1
        if self.submit_error:
            raise OSError("submission failed")
        path, _, kind, _ = self.prepared[-1]
        if self.echo:
            info = {"filename": "another.txt" if self.wrong_name else Path(path).name}
            message = Message(str(self.submits), "附件", attr="self", type=kind, info=info)
            self.state = replace(self.state, messages=(*self.state.messages, message))

    def close(self):
        pass


@pytest.fixture
def media_client(tmp_path):
    file = tmp_path / "test.txt"
    file.write_text("acceptance", encoding="utf-8")
    with WeChat(backend_factory=MediaBackend, send_timeout=0.01) as wx:
        yield wx, wx._backend, file


def test_actual_generic_file_bubble_is_recognized():
    kind, info = message_details("mmui::ChatBubbleItemView", "文件\ntest.txt\n44B\n微信电脑版")
    assert kind == "file" and info["filename"] == "test.txt"
    assert message_details("mmui::ChatTextItemView", "文件\ntest.txt\n44B")[0] == "text"
    assert message_details("mmui::ChatBubbleItemView", "未知卡片")[0] == "other"


def test_native_gif_label_is_recognized_without_treating_replies_as_images():
    kind, info = message_details("mmui::ChatBubbleReferItemView", "动画表情")
    assert kind == "image" and info["animated"] is True
    assert message_details("mmui::ChatBubbleReferItemView", "引用\n动画表情")[0] == "other"
    assert message_details("mmui::ChatBubbleReferItemView", "引用文字")[0] == "other"
    assert message_details("mmui::ChatTextItemView", "动画表情")[0] == "text"


def test_gif_send_confirmation_accepts_the_actual_native_bubble(media_client):
    wx, backend, file = media_client
    image = file.with_suffix(".gif")
    image.write_bytes(b"GIF89a")
    original = backend.submit_attachment

    def submit(who):
        original(who)
        kind, info = message_details("mmui::ChatBubbleReferItemView", "动画表情")
        message = replace(backend.state.messages[-1], type=kind, info=info, content="动画表情")
        backend.state = replace(backend.state, messages=(*backend.state.messages[:-1], message))

    backend.submit_attachment = submit
    result = wx.SendImage(image)
    assert result.kind == "image" and result.verification == "new_image_and_side"
    assert backend.submits == 1


def test_media_target_and_context_are_checked_before_picker(media_client):
    wx, backend, file = media_client
    with pytest.raises(ChatChanged):
        wx.SendFile(file, who="其他好友")
    with pytest.raises(ChatChanged):
        wx.SendFile(file, who="测试好友", expected_context="old")
    assert backend.prepared == [] and backend.submits == 0


def test_file_send_requires_a_new_matching_filename(media_client):
    wx, backend, file = media_client
    result = wx.SendFile(file, who="测试好友")
    assert result and result.verification == "filename_and_side"
    backend.wrong_name = True
    with pytest.raises(DeliveryUncertain):
        wx.SendFile(file)
    assert backend.submits == 2


def test_existing_attachment_draft_is_not_submitted(media_client):
    wx, backend, file = media_client
    backend.prepare_error = True
    with pytest.raises(WeChatError, match="草稿"):
        wx.SendFile(file)
    assert backend.submits == 0


@pytest.mark.parametrize("uncertain", ("no_echo", "submit_error"))
def test_unknown_attachment_delivery_is_not_retried(media_client, uncertain):
    wx, backend, file = media_client
    backend.echo = uncertain != "no_echo"
    backend.submit_error = uncertain == "submit_error"
    with pytest.raises(DeliveryUncertain):
        wx.SendFile(file)
    assert backend.submits == 1


def test_entire_batch_is_prevalidated_before_any_file_is_sent(media_client):
    wx, backend, file = media_client
    with pytest.raises(FileNotFoundError):
        wx.SendFiles([file, file.parent / "missing.txt"])
    with pytest.raises(ValueError):
        wx.SendFiles(str(file))
    assert backend.prepared == [] and backend.submits == 0


def test_partial_batch_exposes_completed_items_without_retry(media_client):
    wx, backend, file = media_client
    second = file.with_name("second.txt")
    second.write_text("second", encoding="utf-8")
    original = backend.submit_attachment

    def submit(who):
        if backend.submits:
            backend.echo = False
        original(who)

    backend.submit_attachment = submit
    with pytest.raises(BatchSendError) as error:
        wx.SendFiles([file, second])
    assert len(error.value.completed) == 1
    assert error.value.failed_path == str(second)
    assert error.value.uncertain and backend.submits == 2


def test_image_confirmation_exposes_its_limited_evidence(media_client):
    wx, _, file = media_client
    image = file.with_suffix(".png")
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = wx.SendImage(image)
    assert result.kind == "image" and result.verification == "new_image_and_side"
    with pytest.raises(ValueError):
        wx.SendImage(file)


@pytest.mark.parametrize(
    "name", ["../../evil.txt", r"C:\other\evil.txt", "CON.txt", "a:b?.jpg", "..."]
)
def test_download_filename_cannot_escape_destination_or_use_devices(name):
    result = safe_filename(name)
    assert "/" not in result and "\\" not in result and ":" not in result
    assert result.split(".")[0].upper() != "CON"
    assert result not in {"", ".", ".."}


def test_only_existing_regular_files_are_selected(tmp_path):
    with pytest.raises(ValueError):
        local_attachment(tmp_path)
    with pytest.raises(FileNotFoundError):
        local_attachment(tmp_path / "missing")


def test_voice_transcription_changes_do_not_change_identity():
    before = "语音 3秒"
    after = "语音 3秒\n转文字：测试识别"
    kind, original = message_details("mmui::ChatVoiceItemView", before)
    _, converted = message_details("mmui::ChatVoiceItemView", after)
    assert converted["transcript"] == "测试识别"
    assert identity_content(kind, before, original) == identity_content(kind, after, converted)


def test_conversion_status_and_ordinary_text_are_not_voice_transcripts():
    _, details = message_details("mmui::ChatVoiceItemView", "语音 3秒\n转换中")
    assert "transcript" not in details
    assert message_details("mmui::ChatTextItemView", "语音 3秒\n测试文字")[0] == "text"


def test_live_voice_duration_unit_is_not_treated_as_a_transcript():
    before = '语音2"秒'
    after = '语音2"秒测试语音识别。'
    kind, original = message_details("mmui::ChatVoiceItemView", before)
    _, converted = message_details("mmui::ChatVoiceItemView", after)
    assert original == {"duration_seconds": 2}
    assert converted["transcript"] == "测试语音识别。"
    assert identity_content(kind, before, original) == identity_content(kind, after, converted)


def test_legitimate_voice_text_can_contain_a_playback_word():
    _, details = message_details("mmui::ChatVoiceItemView", '语音3"秒请播放这首歌。')
    assert details["transcript"] == "请播放这首歌。"
