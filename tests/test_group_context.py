import unittest

from group_context import GroupContextStore


class GroupContextStoreTests(unittest.TestCase):
    def test_groups_are_isolated_and_old_items_expire(self) -> None:
        store = GroupContextStore(max_age_seconds=10)
        store.add("g1", "甲", "旧消息", now=1)
        store.add("g1", "乙", "新消息", now=8)
        store.add("g2", "丙", "另一群", now=8)
        self.assertEqual([m.text for m in store.recent("g1", now=12)], ["新消息"])
        self.assertEqual([m.text for m in store.recent("g2", now=12)], ["另一群"])

    def test_message_count_and_combined_text_limits_are_applied(self) -> None:
        store = GroupContextStore(max_messages=3, max_text_chars=7)
        for index in range(5):
            store.add("g", "成员", str(index) + "abcd", now=index)
        result = store.recent("g", now=5)
        self.assertEqual(len(result), 3)
        self.assertEqual("".join(item.text for item in result), "cd4abcd")

    def test_image_count_is_bounded_and_images_expire_with_context(self) -> None:
        store = GroupContextStore(max_images=1, max_age_seconds=5)
        store.add("g", "甲", "第一张", image_data_url="data:image/png;base64,YQ==", now=1)
        store.add("g", "乙", "第二张", image_data_url="data:image/png;base64,Yg==", now=2)
        items = store.recent("g", now=2)
        self.assertIsNone(items[0].image_data_url)
        self.assertIsNotNone(items[1].image_data_url)
        self.assertEqual(store.recent("g", now=8), [])

    def test_oversized_image_is_dropped_but_text_is_kept(self) -> None:
        store = GroupContextStore(max_image_bytes=3)
        store.add(
            "g", "甲", "仍保留文字", image_data_url="data:image/png;base64,QUJDRA=="
        )
        item = store.recent("g")[0]
        self.assertEqual(item.text, "仍保留文字")
        self.assertIsNone(item.image_data_url)

    def test_addressing_survives_context_trimming(self) -> None:
        store = GroupContextStore(max_text_chars=3, max_images=0)
        store.add("g", "甲", "较长消息", image_data_url="data:image/png;base64,YQ==", addressing="明确@Bot", now=1)
        item = store.recent("g", now=2)[0]
        self.assertEqual(item.addressing, "明确@Bot")
        self.assertIsNone(item.image_data_url)


if __name__ == "__main__":
    unittest.main()
