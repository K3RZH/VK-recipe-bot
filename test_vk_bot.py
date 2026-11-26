import json
import unittest
from unittest.mock import patch

from vk_bot import VkClient, menu_cards, menu_choice


class MenuKeyboardTests(unittest.TestCase):
    def test_menu_sends_vk_keyboard_and_keeps_selected_dish(self):
        cards = menu_cards("Меню", [("Завтрак", [(None, "Гренки")])], ["яйцо"])
        text, buttons = cards[0]
        vk = VkClient("test-key", 123)
        with patch.object(vk, "call") as call:
            vk.send(456, 1, text, buttons=buttons)
        keyboard = json.loads(call.call_args.kwargs["keyboard"])
        self.assertTrue(keyboard["inline"])
        self.assertFalse(keyboard["one_time"])
        button = keyboard["buttons"][0][0]
        self.assertEqual(button["color"], "primary")
        self.assertEqual(button["action"]["type"], "text")
        self.assertEqual(menu_choice(button["action"]["payload"]), ("Гренки", ["яйцо"]))


if __name__ == "__main__":
    unittest.main()
