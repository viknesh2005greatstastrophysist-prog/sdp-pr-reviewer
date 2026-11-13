from flask.json import JSONEncoder


def test_encoder_html():
    class ObjectWithHTML:
        TEST_HTML = "<p>test</p>"

        def __html__(self):
            return self.TEST_HTML

    encoder = JSONEncoder()
    assert isinstance(encoder, JSONEncoder)
    encoder_default = encoder.default(ObjectWithHTML())
    assert isinstance(encoder_default, str)
    assert encoder_default == ObjectWithHTML().TEST_HTML
