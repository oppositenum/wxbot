import unittest

from core import bot


def img(lid, sender="a", t=1000, sid=None):
    return {"type": 3, "local_id": lid, "server_id": sid if sid is not None else lid + 9000,
            "sender": sender, "create_time": t, "is_self": False}


def txt(content, sender="a", t=1010, refer=None):
    m = {"type": 1, "content": content, "sender": sender, "create_time": t, "is_self": False}
    if refer:
        m["refer"] = refer
    return m


class ImageAskTest(unittest.TestCase):
    def key(self, m):
        return (m["server_id"], m["local_id"], 3)

    def test_same_batch_question_looks_at_the_image(self):
        i = img(1)
        self.assertEqual(bot._images_asked_about([txt("这是什么，怎么不回复我")], [i], []), {self.key(i)})
        self.assertEqual(bot._images_asked_about([txt("帮我看看这个")], [i], []), {self.key(i)})

    def test_unrelated_text_does_not_trigger_vision(self):
        self.assertEqual(bot._images_asked_about([txt("晚上吃什么")], [img(1)], []), set())

    def test_recent_image_from_same_sender(self):
        old, mine, other = img(1, t=500), img(2, t=950), img(3, sender="b", t=990)
        got = bot._images_asked_about([txt("图里写的什么", t=1000)], [], [old, mine, other])
        self.assertEqual(got, {self.key(mine)})
        self.assertEqual(bot._images_asked_about([txt("这是啥", t=2000)], [], [mine]), set())   # 太久了

    def test_quoted_image(self):
        q = img(5, sid=-123)
        ref = {"type": "3", "svrid": str(-123 & 0xFFFFFFFFFFFFFFFF)}
        self.assertEqual(bot._images_asked_about([txt("嗯？", refer=ref)], [], [q]), {self.key(q)})


if __name__ == "__main__":
    unittest.main()
