"""Pure-function tests: no vLLM, no sandbox, no server.

Run with:  uv run python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest

from app.testgen import (Case, bounds_for, compare, conforms,
                         detect_style, families, grounded, stub_for, to_spec,
                         validate_spec, within_constraints, _tokens)

COIN = """322. Coin Change
Return the fewest number of coins that you need to make up that amount.
Input: coins = [1,2,5], amount = 11
Output: 3
Constraints:
1 <= coins.length <= 12
0 <= amount <= 10^4
"""

COIN_RAW = {
    "style": "leetcode", "entry": "coinChange",
    "params": [{"name": "coins", "type": "List[int]"},
               {"name": "amount", "type": "int"}],
    "returns": "int", "unordered": False,
    "examples": [{"inputs": {"coins": [1, 2, 5], "amount": 11}, "expected": 3}],
    "constraints": [{"expr": "1 <= coins.length <= 12", "target": "coins.length",
                     "lo": 1, "hi": 12},
                    {"expr": "0 <= amount <= 10^4", "target": "amount",
                     "lo": 0, "hi": 10000}],
}


class TestConformance(unittest.TestCase):
    def test_bool_is_not_an_int(self):
        # The model reaches for True when it means 1 and the confusion is silent.
        self.assertFalse(conforms(True, "int"))
        self.assertTrue(conforms(True, "bool"))

    def test_nested_lists(self):
        self.assertTrue(conforms([[1, 2], [3]], "List[List[int]]"))
        self.assertFalse(conforms([[1, "a"]], "List[List[int]]"))

    def test_int_is_an_acceptable_float(self):
        self.assertTrue(conforms(3, "float"))


class TestValidateSpec(unittest.TestCase):
    def test_good_spec_passes(self):
        self.assertEqual(validate_spec(COIN_RAW, COIN), [])

    def test_flattened_example_is_caught(self):
        # The real failure from the first extraction attempt: three positional
        # values where two named parameters were asked for.
        raw = dict(COIN_RAW, examples=[{"inputs": {"a": 1, "b": 2, "c": 5},
                                        "expected": 3}])
        self.assertTrue(any("do not match the parameters" in b
                            for b in validate_spec(raw, COIN)))

    def test_wrong_declared_type_is_caught(self):
        raw = dict(COIN_RAW, params=[{"name": "coins", "type": "int"},
                                     {"name": "amount", "type": "int"}])
        self.assertTrue(any("is not a int" in b for b in validate_spec(raw, COIN)))

    def test_no_examples_means_no_oracle(self):
        self.assertTrue(any("examples is empty" in b
                            for b in validate_spec(dict(COIN_RAW, examples=[]), COIN)))

    def test_invented_example_is_caught(self):
        raw = dict(COIN_RAW, examples=[{"inputs": {"coins": [7, 13, 91],
                                                   "amount": 8123}, "expected": 4}])
        self.assertTrue(any("not written anywhere" in b
                            for b in validate_spec(raw, COIN)))

    def test_duplicate_parameter_names(self):
        raw = dict(COIN_RAW, params=[{"name": "coins", "type": "List[int]"},
                                     {"name": "coins", "type": "int"}])
        self.assertTrue(any("duplicate" in b for b in validate_spec(raw, COIN)))

    def test_backwards_bounds(self):
        raw = dict(COIN_RAW, constraints=[{"expr": "x", "target": "amount",
                                           "lo": 9, "hi": 2}])
        self.assertTrue(any("exceeds hi" in b for b in validate_spec(raw, COIN)))


class TestGrounding(unittest.TestCase):
    def test_space_separated_statement_still_grounds(self):
        # HackerRank prints its sample array as "10 20 20", not as JSON.
        problem = "Sample Input\n9\n10 20 20 10 10 30 50 10 20\nSample Output\n3"
        self.assertEqual(grounded({"n": 9, "ar": [10, 20, 20, 10]},
                                  _tokens(problem)), 1.0)

    def test_invented_values_score_low(self):
        self.assertLess(grounded([881, 992, 773], _tokens(COIN)), 0.6)


class TestStyle(unittest.TestCase):
    def test_input_format_beats_the_models_guess(self):
        self.assertEqual(detect_style("Input Format\nThe first line...",
                                      "leetcode"), "stdin")

    def test_leetcode_shape(self):
        self.assertEqual(detect_style("Input: nums = [1,2]", "stdin"), "leetcode")


class TestStub(unittest.TestCase):
    def test_leetcode_stub_is_a_class(self):
        s = stub_for(to_spec(COIN_RAW))
        self.assertIn("class Solution:", s)
        self.assertIn("def coinChange(self, coins: List[int], amount: int)", s)

    def test_stdin_stub_parses_its_own_input(self):
        raw = dict(COIN_RAW, style="stdin", entry="sockMerchant",
                   params=[{"name": "n", "type": "int"},
                           {"name": "ar", "type": "List[int]"}])
        s = stub_for(to_spec(raw))
        self.assertIn("n = int(input())", s)
        self.assertIn("ar = list(map(int, input().split()))", s)
        self.assertIn("print(sockMerchant(n, ar))", s)


class TestCompare(unittest.TestCase):
    spec = to_spec(COIN_RAW)

    def test_list_and_tuple_are_equal(self):
        s = to_spec(dict(COIN_RAW, returns="List[int]"))
        self.assertTrue(compare((0, 1), [0, 1], s))

    def test_unordered(self):
        s = to_spec(dict(COIN_RAW, returns="List[int]", unordered=True))
        self.assertTrue(compare([1, 0], [0, 1], s))
        self.assertFalse(compare([1, 0], [0, 1], to_spec(
            dict(COIN_RAW, returns="List[int]"))))

    def test_float_tolerance(self):
        s = to_spec(dict(COIN_RAW, returns="float"))
        self.assertTrue(compare(0.1 + 0.2, 0.3, s))
        self.assertFalse(compare(0.3001, 0.3, s))


class TestBounds(unittest.TestCase):
    spec = to_spec(COIN_RAW)

    def test_length_and_value_bounds_are_kept_apart(self):
        self.assertEqual(bounds_for(self.spec, "coins", size=True), (1, 12))
        self.assertEqual(bounds_for(self.spec, "amount", size=False), (0, 10000))

    def test_unbounded_stays_none(self):
        self.assertEqual(bounds_for(self.spec, "coins", size=False), (None, None))


class TestWithinConstraints(unittest.TestCase):
    spec = to_spec(COIN_RAW)

    def test_accepts_a_legal_input(self):
        self.assertTrue(within_constraints({"coins": [1, 2, 5], "amount": 11},
                                           self.spec))

    def test_rejects_an_over_long_array(self):
        self.assertFalse(within_constraints({"coins": list(range(1, 20)),
                                             "amount": 11}, self.spec))

    def test_rejects_an_out_of_range_scalar(self):
        self.assertFalse(within_constraints({"coins": [1], "amount": -1}, self.spec))

    def test_rejects_a_missing_parameter(self):
        self.assertFalse(within_constraints({"coins": [1]}, self.spec))


class TestFamilies(unittest.TestCase):
    def test_every_generated_input_is_legal(self):
        spec = to_spec(COIN_RAW)
        cases = families(spec, budget=20)
        self.assertTrue(cases)
        for label, inputs in cases:
            self.assertTrue(within_constraints(inputs, spec), f"{label}: {inputs}")

    def test_inputs_are_deduped(self):
        spec = to_spec(COIN_RAW)
        seen = [str(sorted(i.items())) for _, i in families(spec, 20)]
        self.assertEqual(len(seen), len(set(seen)))

    def test_families_are_deterministic(self):
        spec = to_spec(COIN_RAW)
        self.assertEqual(families(spec, 20), families(spec, 20))

    def test_a_family_the_statement_does_not_license_is_skipped(self):
        # coins.length >= 1, so there must be no empty-array case.
        spec = to_spec(COIN_RAW)
        self.assertNotIn("empty", [n for n, _ in families(spec, 20)])

    def test_string_problems_produce_inputs(self):
        raw = {"style": "leetcode", "entry": "lengthOfLongestSubstring",
               "params": [{"name": "s", "type": "str"}], "returns": "int",
               "examples": [{"inputs": {"s": "abcabcbb"}, "expected": 3}],
               "constraints": [{"expr": "0 <= s.length <= 5*10^4",
                                "target": "s.length", "lo": 0, "hi": 50000}]}
        spec = to_spec(raw)
        cases = families(spec, budget=12)
        self.assertTrue(cases)
        # s.length >= 0 licenses the empty string; it arrives as "minimum size"
        # and the "empty" recipe is then deduped away rather than repeated.
        self.assertIn("", [i["s"] for _, i in cases])
        for _, inputs in cases:
            self.assertIsInstance(inputs["s"], str)

    def test_the_array_and_its_length_stay_consistent(self):
        raw = {"style": "stdin", "entry": "sockMerchant",
               "params": [{"name": "n", "type": "int"},
                          {"name": "ar", "type": "List[int]"}],
               "returns": "int",
               "examples": [{"inputs": {"n": 3, "ar": [1, 1, 2]}, "expected": 1}],
               "constraints": [{"expr": "1 <= n <= 100", "target": "n",
                                "lo": 1, "hi": 100},
                               {"expr": "1 <= ar[i] <= 100", "target": "ar[i]",
                                "lo": 1, "hi": 100}]}
        spec = to_spec(raw)
        for label, inputs in families(spec, budget=10):
            self.assertEqual(inputs["n"], len(inputs["ar"]), label)


class TestSuiteShape(unittest.TestCase):
    def test_hidden_expected_never_leaks_without_an_oracle(self):
        from app.testgen import TestSuite
        s = TestSuite("none", "references disagreed",
                      spec=to_spec(COIN_RAW), examples=to_spec(COIN_RAW).examples,
                      hidden=(Case("trap", {"coins": [1, 3, 4], "amount": 6}, 2),))
        d = s.as_dict()
        self.assertEqual(d["hidden"], [])
        self.assertIn("expected", d["examples"][0])   # the statement's own, fine


if __name__ == "__main__":
    unittest.main()


class TestRetargetConstraints(unittest.TestCase):
    def test_hackerrank_length_variable_binds_to_its_array(self):
        from app.testgen import retarget_constraints
        raw = {"params": [{"name": "k", "type": "int"},
                          {"name": "A", "type": "List[int]"}],
               "constraints": [{"expr": "1 <= n <= 10", "target": "n",
                                "lo": 1, "hi": 10}]}
        problem = "cookies has the following parameters:\n  int A[n]: sweetness"
        out = retarget_constraints(raw, problem)
        self.assertEqual(out["constraints"][0]["target"], "A.length")
        self.assertEqual(bounds_for(to_spec(dict(out, style="leetcode",
                                                 entry="cookies", returns="int",
                                                 examples=[])), "A", size=True),
                         (1, 10))

    def test_a_real_parameter_is_left_alone(self):
        from app.testgen import retarget_constraints
        raw = {"params": [{"name": "n", "type": "int"},
                          {"name": "ar", "type": "List[int]"}],
               "constraints": [{"expr": "1 <= n <= 100", "target": "n",
                                "lo": 1, "hi": 100}]}
        out = retarget_constraints(raw, "int ar[n]: the colors")
        self.assertEqual(out["constraints"][0]["target"], "n")
