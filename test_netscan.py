"""Offline tests for window folding and the verdict: python3 -m unittest test_netscan"""
import unittest

import netscan


def recs(pattern):
    """pattern: {key: '1101...'} one char per second, 1 = answered."""
    out = []
    for key, s in pattern.items():
        hop = 1 if key == "gw" else int(key[3:]) if key.startswith("hop") else None
        for i, ch in enumerate(s):
            out.append({"key": key, "hop": hop, "round": i, "seq": 0, "t": i, "p": 0,
                        "rtt": 5.0 + i % 3 if ch == "1" else None, "ip": "x", "err": None})
    return out


class Fold(unittest.TestCase):
    def test_blackouts_attributed_to_furthest_responding_point(self):
        w = netscan.fold_window(recs({
            "gw":           "1111011111",
            "hop2":         "1111001111",
            "hop4":         "1111001110",
            "inet:1.1.1.1": "1000000111",
            "inet:8.8.8.8": "1000000011",
        }))
        # rounds 1-3: gw+isp fine -> upstream; round 4: gw ok, isp lost -> access; round 5: gw lost -> lan; 6: upstream
        self.assertEqual(w["dead"], {"lan": 1, "access": 1, "upstream": 4})
        self.assertEqual(w["dead_s"], 6)
        self.assertEqual(w["isp_key"], "hop2")
        self.assertEqual(w["inet"]["sent"], 20)
        self.assertEqual(w["inet"]["loss"], 65.0)
        self.assertEqual(w["inet"]["burst"], 6)
        self.assertEqual(w["per"]["gw"]["loss"], 10.0)

    def test_one_target_dropping_is_not_a_blackout(self):
        w = netscan.fold_window(recs({"gw": "1111", "inet:1.1.1.1": "0000", "inet:8.8.8.8": "1111"}))
        self.assertEqual(w["dead_s"], 0)
        self.assertEqual(w["inet"]["loss"], 50.0)


class Verdict(unittest.TestCase):
    def hop(self, label, role, loss):
        return {"label": label, "role": role, "loss": loss, "sent": 100}

    def test_rate_limited_middle_hop_is_ignored(self):
        v = netscan.verdict([self.hop("Your router", "router", 0), self.hop("Hop 2", "isp", 0.1),
                             self.hop("Hop 3", "hop", 70)], {"sent": 100, "loss": 0.1}, {})
        self.assertEqual(v["level"], "good")

    def test_loss_starting_at_isp_hop(self):
        v = netscan.verdict([self.hop("Your router", "router", 0.1), self.hop("Hop 2", "isp", 4),
                             self.hop("Hop 3", "hop", 60), self.hop("Hop 4", "hop", 4.5)],
                            {"sent": 100, "loss": 5}, {"lan": 0, "access": 30, "upstream": 2})
        self.assertEqual(v["where"], "isp")

    def test_loss_starting_at_router(self):
        v = netscan.verdict([self.hop("Your router", "router", 5), self.hop("Hop 2", "isp", 5)],
                            {"sent": 100, "loss": 5}, {})
        self.assertEqual(v["where"], "router")


class RateLimited(unittest.TestCase):
    def test_only_loss_that_does_not_carry_on_is_flagged(self):
        chain = [{"loss": 0.1}, {"loss": 0.2}, {"loss": 68.3}, {"loss": 0.2}, {"loss": 0.1}]
        netscan.mark_rate_limited(chain)
        self.assertEqual([h["limited"] for h in chain], [False, False, True, False, False])

    def test_loss_that_persists_is_not_flagged(self):
        chain = [{"loss": 0}, {"loss": 5}, {"loss": 60}, {"loss": 5}, {"loss": 5.2}]
        netscan.mark_rate_limited(chain)
        self.assertEqual([h["limited"] for h in chain], [False, False, True, False, False])
        chain = [{"loss": 0}, {"loss": 5}, {"loss": 4.5}]
        netscan.mark_rate_limited(chain)
        self.assertFalse(any(h["limited"] for h in chain))


class Classify(unittest.TestCase):
    def test_status(self):
        c = netscan.Collector.__new__(netscan.Collector)
        ok = {"inet_sent": 90, "inet_loss": 0, "http_ok": 1, "dns_ok": 1, "inet_rtt": 10, "dead_s": 0}
        self.assertEqual(c.classify(ok, "en0"), "ok")
        self.assertEqual(c.classify(dict(ok, inet_loss=100, http_ok=0), "en0"), "offline")
        self.assertEqual(c.classify(dict(ok, dead_s=5), "en0"), "degraded")
        self.assertEqual(c.classify(dict(ok, inet_loss=4), "en0"), "degraded")
        self.assertEqual(c.classify(ok, None), "no-network")


if __name__ == "__main__":
    unittest.main()
