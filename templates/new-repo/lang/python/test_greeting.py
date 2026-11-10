from greeting import greeting


def test_greeting():
	assert greeting("x") == "hello, x"
