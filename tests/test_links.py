from msgbridge.links import clean, clean_text


def test_tracking_parameters_are_removed():
    assert clean("https://shop.example.com/item?id=42&utm_source=ig&utm_medium=social&fbclid=abc") == \
        "https://shop.example.com/item?id=42"
    assert clean("https://youtu.be/dQw4w9WgXcQ?si=sharer123&t=42") == "https://youtu.be/dQw4w9WgXcQ?t=42"


def test_ordinary_links_are_kept_whole():
    url = "https://www.nytimes.com/2026/10/20/travel/bali.html"
    assert clean(url) == url
    assert clean("https://maps.app.goo.gl/AbC123") == "https://maps.app.goo.gl/AbC123"


def test_access_links_become_private_placeholders():
    assert clean("https://docs.google.com/document/d/1abc/edit?usp=sharing") == "[private link: docs.google.com]"
    assert clean("https://us02web.zoom.us/j/8123?pwd=xyz") == "[private link: us02web.zoom.us]"
    assert clean("https://venmo.com/u/someone") == "[private link: venmo.com]"
    assert clean("https://files.example.com/get?token=secret123") == "[private link: files.example.com]"


def test_sentence_punctuation_stays_outside_the_link():
    text = "see https://example.com/a?utm_source=x. also (https://example.com/b) and https://en.wikipedia.org/wiki/Foo_(bar)!"
    assert clean_text(text) == "see https://example.com/a. also (https://example.com/b) and https://en.wikipedia.org/wiki/Foo_(bar)!"
