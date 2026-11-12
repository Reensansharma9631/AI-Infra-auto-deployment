import importlib.util, pathlib, unittest

p = pathlib.Path(__file__).parent.parent / "ansible/roles/mysql_ha/files/dbha_agent.py"
spec = importlib.util.spec_from_file_location("dbha_agent", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

CFG = {"fail_grace": 25, "fence_after": 12, "allow_single_survivor": False}
A, B, C = "10.0.0.1", "10.0.0.2", "10.0.0.3"


def node(sid, up=True, writable=False, gtid="u:1-10", epoch=0, seen=None):
    return {"up": up, "writable": writable, "gtid": gtid, "server_id": sid, "epoch": epoch, "primary_seen": seen}


class Gtid(unittest.TestCase):
    def test_count(self):
        self.assertEqual(m.gtid_count(""), 0)
        self.assertEqual(m.gtid_count("u:1-5"), 5)
        self.assertEqual(m.gtid_count("u:1-5:7-9,v:3\n,w:1-2"), 5 + 3 + 1 + 2)


class Decide(unittest.TestCase):
    def setUp(self):
        self.st = {"no_primary_since": None, "no_quorum_since": None}

    def d(self, me, view, now):
        return m.decide(me, view, 3, CFG, self.st, now)

    def test_bootstrap_lowest_server_id_wins_after_grace(self):
        v = {A: node(1), B: node(2), C: node(3)}
        self.assertEqual(self.d(A, v, 100)[0], "wait")          # starts the grace timer
        self.assertEqual(self.d(A, v, 110)[0], "wait")          # still inside grace
        self.assertEqual(self.d(A, v, 130), ("promote", A))     # grace over, A is the best candidate
        self.assertEqual(self.d(B, v, 130), ("wait", A))        # B must not promote

    def test_most_up_to_date_replica_is_promoted(self):
        v = {A: node(1, up=False), B: node(2, gtid="u:1-90"), C: node(3, gtid="u:1-100")}
        self.d(C, v, 0); self.d(B, v, 0)
        self.assertEqual(self.d(C, v, 30), ("promote", C))
        self.assertEqual(self.d(B, v, 30), ("wait", C))

    def test_no_promotion_without_quorum(self):
        v = {A: node(1, up=False), B: node(2, up=False), C: node(3)}
        for t in (0, 30, 60):
            self.assertEqual(self.d(C, v, t)[0], "wait")

    def test_single_survivor_allowed_flag(self):
        cfg = {**CFG, "allow_single_survivor": True}
        st = {"no_primary_since": None, "no_quorum_since": None}
        v = {A: node(1, up=False), B: node(2, up=False), C: node(3)}
        m.decide(C, v, 3, cfg, st, 0)
        self.assertEqual(m.decide(C, v, 3, cfg, st, 30), ("promote", C))

    def test_partitioned_replica_does_not_promote_if_peer_still_sees_primary(self):
        # C lost the link to A (primary) but B still sees A
        v = {A: node(1, up=False), B: node(2, seen=A), C: node(3)}
        for t in (0, 30, 60):
            self.assertEqual(self.d(C, v, t)[0], "wait")

    def test_replicas_follow_single_writer(self):
        v = {A: node(1, writable=True), B: node(2), C: node(3)}
        self.assertEqual(self.d(B, v, 0), ("follow", A))
        self.assertEqual(self.d(A, v, 0), ("keep_primary", A))

    def test_isolated_primary_fences_before_others_elect(self):
        v = {A: node(1, writable=True), B: node(2, up=False), C: node(3, up=False)}
        self.assertEqual(self.d(A, v, 0), ("keep_primary", A))
        self.assertEqual(self.d(A, v, 11)[0], "keep_primary")
        self.assertEqual(self.d(A, v, 12), ("fence", A))
        self.assertLess(CFG["fence_after"], CFG["fail_grace"])   # fencing must beat election

    def test_split_brain_newest_epoch_wins_and_old_primary_demotes(self):
        v = {A: node(1, writable=True, epoch=1, gtid="u:1-120"), B: node(2, writable=True, epoch=2, gtid="u:1-100"),
             C: node(3)}
        self.assertEqual(self.d(A, v, 0), ("demote_follow", B))
        self.assertEqual(self.d(B, v, 0), ("keep_primary", B))
        self.assertEqual(self.d(C, v, 0), ("follow", B))

    def test_split_brain_same_epoch_more_data_wins(self):
        v = {A: node(1, writable=True, gtid="u:1-100"), B: node(2, writable=True, gtid="u:1-110"), C: node(3)}
        self.assertEqual(self.d(B, v, 0), ("keep_primary", B))
        self.assertEqual(self.d(A, v, 0), ("demote_follow", B))


if __name__ == "__main__":
    unittest.main()
