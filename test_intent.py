import json
import unittest
from unittest.mock import patch

from intent import understand_request


class PurchaseConstraintTests(unittest.TestCase):
    def route(self, message, constraints, *, ingredients=None, excluded=None, can_buy=None, max_extra=None):
        inferred = {
            "mode": "dish", "dish": "гренки", "ingredients": ingredients or [],
            "excluded": excluded or [], "can_buy": can_buy, "max_extra": max_extra,
        }
        with patch("intent.structured_chat", side_effect=[json.dumps(inferred), json.dumps(constraints)]):
            return understand_request("test-model", message, allow_menu=True)[2]

    def test_unstated_purchase_does_not_filter_dish(self):
        intent = self.route("хочу гренки сделать", {
            "purchase_limit": False, "purchase_list": False,
        }, ingredients=["хлеб"], excluded=["ничего"], can_buy=["масло"])
        self.assertIsNone(intent["can_buy"])
        self.assertIsNone(intent["max_extra"])
        self.assertEqual(intent["excluded"], [])

    def test_explicit_no_purchase_and_food_exclusion_are_preserved(self):
        intent = self.route("Гренки без яиц, покупать ничего не буду", {
            "purchase_limit": True, "purchase_list": False,
        }, ingredients=["хлеб"], excluded=["яйцо"], can_buy=["нет"], max_extra=0)
        self.assertEqual(intent["can_buy"], [])
        self.assertEqual(intent["max_extra"], 0)
        self.assertEqual(intent["excluded"], ["яйцо"])

    def test_count_does_not_become_purchase_list(self):
        intent = self.route("Хочу гренки, могу докупить два продукта", {
            "purchase_limit": True, "purchase_list": False,
        }, can_buy=["2 продукта"], max_extra=2)
        self.assertEqual(intent["max_extra"], 2)
        self.assertIsNone(intent["can_buy"])

    def test_named_purchase_list_is_preserved(self):
        intent = self.route("Гренки без яиц, докуплю только масло", {
            "purchase_limit": False, "purchase_list": True,
        }, excluded=["яйцо"], can_buy=["масло"], max_extra=1)
        self.assertIsNone(intent["max_extra"])
        self.assertEqual(intent["can_buy"], ["масло"])
        self.assertEqual(intent["excluded"], ["яйцо"])


if __name__ == "__main__":
    unittest.main()
