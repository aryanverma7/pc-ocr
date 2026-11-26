import idle_gate
from idle_gate import IdleGate


def _gate(samples, busy=lambda: False, game=lambda: False):
    t = [0.0]
    it = iter(samples)
    g = IdleGate(sample=lambda: next(it), busy=busy, game=game, clock=lambda: t[0])
    return g, t


def _feed(g, t, n):
    for _ in range(n):
        g.observe()
        t[0] += 10


def test_not_ready_before_600s_then_ready():
    g, t = _gate([5] * 100)
    _feed(g, t, 60)  # observed 0..590, clock now 600
    t[0] = 590
    assert not g.ready()
    t[0] = 600
    assert g.ready()


def test_one_busy_sample_resets_window():
    g, t = _gate([5] * 30 + [40] + [5] * 100)
    _feed(g, t, 61)
    assert not g.ready()  # window restarted at t=310
    _feed(g, t, 30)
    assert g.ready()


def test_none_sample_resets_window():
    g, t = _gate([5] * 30 + [None] + [5] * 100)
    _feed(g, t, 61)
    assert not g.ready()


def test_fullscreen_or_game_resets_and_interrupts():
    flag = {"busy": False, "game": False}
    g, t = _gate([5] * 200, busy=lambda: flag["busy"], game=lambda: flag["game"])
    _feed(g, t, 61)
    assert g.ready() and not g.interrupt()
    flag["game"] = True
    assert g.interrupt()
    _feed(g, t, 1)
    assert not g.ready()
    flag["game"], flag["busy"] = False, True
    assert g.interrupt()


def test_game_running_parses_tasklist():
    out = ('"System Idle Process","0","Services","0","8 K"\r\n'
           '"VALORANT-Win64-Shipping.exe","1234","Console","1","2,000,000 K"\r\n')
    assert idle_gate.game_running(["valorant-win64-shipping.exe"], out)
    assert not idle_gate.game_running(["VALORANT.exe"], out)
    assert not idle_gate.game_running(idle_gate.DEFAULT_GAMES, '"explorer.exe","1","Console","1","1 K"\r\n')
