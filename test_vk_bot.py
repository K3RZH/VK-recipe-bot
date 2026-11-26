import json
import unittest
from unittest.mock import patch

from recipe_photos import find_photo, photo_search
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


class RecipePhotoTests(unittest.TestCase):
    def test_shchi_photo_is_selected_with_its_license(self):
        page = {"imageinfo": [{
            "mime": "image/jpeg",
            "thumburl": "https://thumb.wikimedia.org/Shchi.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Shchi.jpg",
            "extmetadata": {
                "Artist": {"value": "Victoria Vasilieva"},
                "LicenseShortName": {"value": "CC BY-SA 2.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/2.0"},
            },
        }]}
        with patch("recipe_photos.commons_query", return_value=[page]) as commons:
            photo = find_photo({"title": "Русские щи из свежей капусты"})
        self.assertEqual(commons.call_args.args[0], "Shchi.jpg")
        self.assertEqual(photo["page"], "https://commons.wikimedia.org/wiki/File:Shchi.jpg")
        self.assertEqual(photo["license"], "CC BY-SA 2.0")

    def test_sorrel_shchi_uses_its_own_photo_search(self):
        self.assertEqual(photo_search("Зелёные щи со щавелем"), ("", "Зелёные щи со щавелем"))


if __name__ == "__main__":
    unittest.main()
