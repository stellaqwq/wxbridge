from wxbridge.pixels import Frame, avatar_fingerprint, classify, classify_media


def frame(background=(245, 245, 245), bubble=None, box=(70, 12, 130, 46)):
    width, height = 300, 60
    r, g, b = background
    pixels = bytearray(bytes((b, g, r, 255)) * width * height)
    if bubble:
        left, top, right, bottom = box
        r, g, b = bubble
        for y in range(top, bottom):
            for x in range(left, right):
                at = (y * width + x) * 4
                pixels[at : at + 4] = bytes((b, g, r, 255))
    return Frame(0, 0, width, height, bytes(pixels))


def test_default_light_own_green_bubble():
    assert (
        classify(frame(bubble=(149, 236, 105), box=(170, 12, 235, 46)), (0, 0, 300, 60)) == "self"
    )


def test_default_light_incoming_white_bubble():
    assert classify(frame(bubble=(255, 255, 255)), (0, 0, 300, 60)) == "friend"


def test_current_weixin_gray_incoming_bubble():
    assert (
        classify(frame(background=(250, 250, 250), bubble=(238, 238, 240)), (0, 0, 300, 60))
        == "friend"
    )


def test_pixels_have_explicit_top_down_coordinates():
    image = Frame(5, 7, 1, 2, bytes((0, 0, 255, 255, 255, 0, 0, 255)))
    assert image.rgb(5, 7) == (255, 0, 0)
    assert image.rgb(5, 8) == (0, 0, 255)


def test_neutral_bubble_on_right_is_not_misread_as_incoming():
    image = frame(background=(250, 250, 250), bubble=(238, 238, 240), box=(170, 12, 235, 46))
    assert classify(image, (0, 0, 300, 60)) == "other"


def test_unknown_theme_and_clipped_row_are_not_guessed():
    assert classify(frame(background=(30, 30, 30)), (0, 0, 300, 60)) == "other"
    assert classify(frame(bubble=(255, 255, 255)), (0, -10, 300, 60)) == "other"
    assert classify(frame(), (0, 0, 300, 60)) == "other"


def test_image_colours_alone_cannot_be_mistaken_for_sender():
    image = frame(background=(250, 250, 250), bubble=(149, 236, 105), box=(70, 12, 235, 46))
    assert classify_media(image, (0, 0, 300, 60)) == "other"


def test_media_side_uses_only_an_unambiguous_avatar_gutter():
    own = frame(background=(250, 250, 250), bubble=(255, 255, 255), box=(244, 8, 272, 40))
    friend = frame(background=(250, 250, 250), bubble=(255, 255, 255), box=(24, 8, 52, 40))
    assert classify_media(own, (0, 0, 300, 60)) == "self"
    assert classify_media(friend, (0, 0, 300, 60)) == "friend"


def test_group_avatar_alias_ignores_message_body_and_rejects_clipping():
    avatar = frame(bubble=(150, 30, 60), box=(24, 8, 52, 40))
    pixels = bytearray(avatar.data)
    for y in range(12, 46):
        for x in range(70, 130):
            at = (y * avatar.width + x) * 4
            pixels[at : at + 4] = bytes((200, 160, 30, 255))
    body = Frame(0, 0, avatar.width, avatar.height, bytes(pixels))
    assert avatar_fingerprint(avatar, (0, 0, 300, 60)) == avatar_fingerprint(body, (0, 0, 300, 60))
    other = frame(bubble=(30, 150, 60), box=(24, 8, 52, 40))
    assert avatar_fingerprint(avatar, (0, 0, 300, 60)) != avatar_fingerprint(other, (0, 0, 300, 60))
    assert avatar_fingerprint(avatar, (0, -10, 300, 60)) == ""
