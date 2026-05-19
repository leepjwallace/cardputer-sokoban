"""Sokoban for the Cardputer-Adv.

Push the cream-colored boxes onto the orange goal markers. The level
is solved when every box is sitting on a goal (the boxes light up
ORANGE to confirm). Three levels of escalating challenge, plus a
one-time character-pick screen at start so the player chooses a
King / Princess / Prince / Knight sprite for the session.

### Visual style

Same vocabulary as snake.py: 20 px DARK header with the ORANGE
hairline at y=20, blocky filled-rect tiles in the BLACK / ORANGE /
CREAM / GRAY_MID palette, header score line on the right. The cell
size is 12 px (snake uses 10) so the biggest level — 4 boxes plus
4 goals at 10x9 — fits inside the 240x135 panel with the level
centered in the play area.

### Controls (mirrors snake.py)

- WASD or `;` `,` `.` `/`  — move / push
- R                         — restart the current level
- U                         — undo the last move
- N or Enter                — go to the next level (only on the
                              "level solved" screen)
- Q or Esc                  — exit back to the launcher

### Layout math

    Header:        y=0..19    (20 px DARK bg, ORANGE hairline at y=20)
    Play area:     y=22..129  (108 px tall, 9 rows x 12 px)
                   x=0..239   (240 wide, 20 cols x 12 px)
    Bottom edge:   y=130..134 (5 px padding, black)

Levels smaller than the 20x9 grid are centered via _ofs_x / _ofs_y;
cells outside the level remain BLACK.
"""

import time

import M5
import machine
from hardware import MatrixKeyboard


# ---- palette (inlined from ui_theme; matches snake / claude_buddy).
# _SKIN is sokoban-only — the detailed picker sprites need a face
# color and we don't want to drag a new palette entry into the
# shared buddy stack just for a 72x72 portrait.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xFF0000
_SKIN = 0xE8B080

_LCD = M5.Lcd

_W = 240
_H = 135

_CELL = 12
_GRID_W = 20        # 240 / 12
_GRID_H = 9         # ~108 / 12, below the header hairline
_PLAY_X = 0
_PLAY_Y = 22        # just below the header hairline at y=20


# Level data. Symbols follow the de-facto Sokoban convention:
#   '#'  wall        '.'  goal        '$'  box
#   '@'  player      '*'  box on goal '+'  player on goal
#   ' '  floor
# Each level is a list of equal-length strings (we don't reflow or
# pad here — _parse_level trusts the input).
_LEVELS = (
    # Level 1 — "First Push". One box, one goal, straight line. The
    # whole point is to teach the push mechanic (you can't pull boxes
    # toward you, only shove them forward).
    (
        "#######",
        "#     #",
        "#  .  #",
        "#     #",
        "#  $  #",
        "#     #",
        "#  @  #",
        "#######",
    ),
    # Level 2 — "Two Up". Two boxes side-by-side, two goals above.
    # Either box can go to either goal; the player walks back around
    # to push the second one after committing the first.
    (
        "########",
        "#      #",
        "#  ..  #",
        "#      #",
        "#  $$  #",
        "#      #",
        "#  @   #",
        "########",
    ),
    # Level 3 — "Four Up". Four boxes, four goals lined up directly
    # overhead. No internal walls — the step up from L2 is "more
    # cargo", not "more puzzle". The player just has to walk back
    # around each box to push it from below.
    #
    # The earlier draft of this level was missing its bottom wall
    # (the player row was the last line), so a 'down' input let the
    # player walk off the level. Always pad the level rectangle with
    # walls on all four sides — _try_move only checks the walls set,
    # so a missing border = no boundary.
    (
        "##########",
        "#  ....  #",
        "#        #",
        "#  $$$$  #",
        "#        #",
        "#        #",
        "#        #",
        "#   @    #",
        "##########",
    ),
    # Level 4 — "Around the Pillar". The old Level 3 idea, but with
    # the bottom wall in place and the player starting in the center
    # so the four boxes form a ring around them. Wall pillars in the
    # middle of rows 2 and 6 force the player to detour through the
    # corridor when moving between the upper and lower box pairs.
    (
        "##########",
        "#  .  .  #",
        "#   ##   #",
        "#  $  $  #",
        "#   @    #",
        "#  $  $  #",
        "#   ##   #",
        "#  .  .  #",
        "##########",
    ),
    # Level 5 — "Twin Barriers". Goals are at the four outer corners,
    # so no box can be solved with a single straight push — each one
    # needs at least one vertical push and one horizontal push, and
    # a full wall barrier across the middle splits the play area so
    # the player has to walk around through the col-1 / col-8 gaps
    # whenever they switch halves. Plenty of deadlock potential if
    # boxes get shoved into the wrong corner.
    (
        "##########",
        "#.      .#",
        "#        #",
        "#  $  $  #",
        "#  ####  #",
        "#  $  $  #",
        "#        #",
        "#.  @   .#",
        "##########",
    ),
)


# Display names for the character-pick screen. Index matches the
# `char_idx` passed into _draw_player below. Keep this tuple in sync
# with the sprite-drawing branches.
_CHARACTERS = ("King", "Queen", "Prince", "Princess")


# ---- centering offsets, recomputed per level by _set_offsets.
# Module-level because _cell_xy is called from a dozen places and
# threading them everywhere would just be noise.
_ofs_x = 0
_ofs_y = 0


def _set_font():
    """Match snake / claude_buddy's font choice so the suite looks
    consistent. DejaVu9 is 10 px tall, comfortable in the 20 px header."""
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("sokoban: setFont fallback:", e)


def _set_offsets(level_w, level_h):
    global _ofs_x, _ofs_y
    _ofs_x = (_GRID_W - level_w) // 2
    _ofs_y = (_GRID_H - level_h) // 2


def _cell_xy(cx, cy):
    """Top-left pixel of cell (cx, cy) in the current level's frame."""
    return (
        _PLAY_X + (cx + _ofs_x) * _CELL,
        _PLAY_Y + (cy + _ofs_y) * _CELL,
    )


def _parse_level(rows):
    """Return (walls, goals, boxes, player) for a level definition.

    Sets are used for walls/goals/boxes so collision checks (which run
    on every keypress) are O(1) lookups. Boxes get returned as a fresh
    set so the caller can mutate it without affecting the source data."""
    walls = set()
    goals = set()
    boxes = set()
    player = (0, 0)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "#":
                walls.add((x, y))
            elif ch == ".":
                goals.add((x, y))
            elif ch == "$":
                boxes.add((x, y))
            elif ch == "*":
                boxes.add((x, y))
                goals.add((x, y))
            elif ch == "@":
                player = (x, y)
            elif ch == "+":
                player = (x, y)
                goals.add((x, y))
    return walls, goals, boxes, player


# ---- per-tile drawing primitives. Each is keyed on the cell coord
# (not pixel coord) and uses _cell_xy to resolve. Cells are 12 px so
# the inset / outline math leaves 10 px of payload after a 1 px border.

def _draw_floor(cx, cy):
    x, y = _cell_xy(cx, cy)
    _LCD.fillRect(x, y, _CELL, _CELL, _BLACK)


def _draw_wall(cx, cy):
    # Bright GRAY_MID outer with a DARK inset — same crisp-edge feel
    # snake's grid has between adjacent segments, applied here so a
    # run of walls reads as tiled brick rather than a solid slab.
    x, y = _cell_xy(cx, cy)
    _LCD.fillRect(x, y, _CELL, _CELL, _GRAY_MID)
    _LCD.fillRect(x + 1, y + 1, _CELL - 2, _CELL - 2, _DARK)


def _draw_goal(cx, cy):
    x, y = _cell_xy(cx, cy)
    _LCD.fillRect(x, y, _CELL, _CELL, _BLACK)
    # 4x4 ORANGE diamond centered in the cell. Big enough to read at
    # arm's length, small enough that a player tile rendered on top
    # of a goal still obviously stands "on" the marker.
    _LCD.fillRect(x + _CELL // 2 - 2, y + _CELL // 2 - 2, 4, 4, _ORANGE)


def _draw_box(cx, cy, on_goal):
    x, y = _cell_xy(cx, cy)
    _LCD.fillRect(x, y, _CELL, _CELL, _BLACK)
    if on_goal:
        # Box parked on its goal. Solid ORANGE — the same "completed"
        # color the goal marker uses, so a glance at the board reads
        # the count of orange tiles as "boxes placed".
        _LCD.fillRect(x + 1, y + 1, _CELL - 2, _CELL - 2, _ORANGE)
    else:
        # In-flight box: CREAM body with an ORANGE border. Echoes the
        # snake / food contrast (cream center on a dark grid).
        _LCD.fillRect(x + 1, y + 1, _CELL - 2, _CELL - 2, _CREAM)
        _LCD.drawRect(x + 1, y + 1, _CELL - 2, _CELL - 2, _ORANGE)


def _draw_player(cx, cy, char_idx, on_goal=False):
    """In-game player tile, composed of 3-5 fillRect calls per character
    so it renders fast and stays in the snake/buddy palette. At 12x12
    we can't fit real faces — silhouette + headpiece carries the cue:

      King     — three-point ORANGE crown on a CREAM body
      Queen    — single ORANGE peak over CREAM hair, ORANGE dress with
                 two side strands of long cream hair
      Prince   — wide CREAM hair on an ORANGE coat (short hair, no crown)
      Princess — small CREAM tiara with an ORANGE jewel on an ORANGE dress

    The detailed-portrait versions are in _draw_preview_*, used only
    on the character-pick screen.

    When the player stands on a goal we drop a tiny ORANGE pip in the
    bottom-right corner so the goal indicator still reads through the
    sprite (which otherwise covers the centered diamond).
    """
    x, y = _cell_xy(cx, cy)
    _LCD.fillRect(x, y, _CELL, _CELL, _BLACK)
    if on_goal:
        _LCD.fillRect(x + _CELL - 3, y + _CELL - 3, 2, 2, _ORANGE)
    if char_idx == 0:
        # King — broad crown on a cream body.
        _LCD.fillRect(x + 2, y + 5, 8, 6, _CREAM)
        _LCD.fillRect(x + 2, y + 2, 2, 3, _ORANGE)
        _LCD.fillRect(x + 5, y + 1, 2, 4, _ORANGE)
        _LCD.fillRect(x + 8, y + 2, 2, 3, _ORANGE)
    elif char_idx == 1:
        # Queen — long-hair side strands + single crown peak on orange dress.
        _LCD.fillRect(x + 2, y + 5, 8, 6, _ORANGE)
        _LCD.fillRect(x + 2, y + 4, 1, 5, _CREAM)
        _LCD.fillRect(x + 9, y + 4, 1, 5, _CREAM)
        _LCD.fillRect(x + 3, y + 3, 6, 2, _CREAM)
        _LCD.fillRect(x + 5, y + 0, 2, 3, _ORANGE)
    elif char_idx == 2:
        # Prince — short cream hair on an orange coat.
        _LCD.fillRect(x + 2, y + 5, 8, 6, _ORANGE)
        _LCD.fillRect(x + 3, y + 2, 6, 3, _CREAM)
    else:
        # Princess — small tiara with single jewel on an orange dress.
        _LCD.fillRect(x + 2, y + 5, 8, 6, _ORANGE)
        _LCD.fillRect(x + 4, y + 2, 4, 3, _CREAM)
        _LCD.fillRect(x + 5, y + 1, 2, 1, _ORANGE)


def _redraw_cell(cx, cy, walls, goals, boxes, player, char_idx):
    """Resolve what should be at (cx, cy) given current state and draw
    it. Used for partial repaints after a move."""
    if (cx, cy) in walls:
        _draw_wall(cx, cy)
    elif (cx, cy) == player:
        _draw_player(cx, cy, char_idx, on_goal=(cx, cy) in goals)
    elif (cx, cy) in boxes:
        _draw_box(cx, cy, on_goal=(cx, cy) in goals)
    elif (cx, cy) in goals:
        _draw_goal(cx, cy)
    else:
        _draw_floor(cx, cy)


def _draw_level(walls, goals, boxes, player, char_idx, level_w, level_h):
    for y in range(level_h):
        for x in range(level_w):
            _redraw_cell(x, y, walls, goals, boxes, player, char_idx)


def _draw_chrome(level_idx, moves):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Sokoban  L{}".format(level_idx + 1), 6, 5)
    _update_moves(moves)


def _update_moves(moves):
    # Right-side score panel only — leaves the title alone so we don't
    # flash "Sokoban Lx" every keypress.
    _LCD.fillRect(120, 0, _W - 120, 20, _DARK)
    _LCD.setTextColor(_CREAM, _DARK)
    text = "moves: {}".format(moves)
    x = _W - 6 - _LCD.textWidth(text)
    _LCD.drawString(text, x, 5)


def _intent(k):
    """Collapse a MatrixKeyboard return into a single gameplay intent.

    Mirrors snake.py's mapping so muscle memory carries between the
    two games:
      WASD       — directional (gamer-default scheme)
      ; , . /    — directional (arrow-cluster scheme; the Cardputer-Adv
                   prints arrow glyphs on these but the matrix driver
                   reports the underlying ASCII)
      R          — restart the level (or replay-all on the all-done screen)
      U          — undo
      N / Enter  — "advance" (next level on solve screen, confirm on pick)
      Q / Esc    — exit
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return "exit"
        # Enter is LF (0x0A) on this firmware build; accept CR too for
        # forward compat (see snake.py for the same note).
        if k in (0x0A, 0x0D):
            return "advance"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "w" or ch == ";":
        return "up"
    if ch == "s" or ch == ".":
        return "down"
    if ch == "a" or ch == ",":
        return "left"
    if ch == "d" or ch == "/":
        return "right"
    if ch == "q":
        return "exit"
    if ch == "r":
        return "restart"
    if ch == "u":
        return "undo"
    if ch == "n":
        return "advance"
    return None


def _try_move(dx, dy, walls, boxes, player):
    """Resolve a directional intent into a new state.

    Returns (new_player, new_boxes, dirty_cells) on success, or None
    if the move is blocked (wall, or pushing into a wall / second box).
    dirty_cells is the list of (cx, cy) the renderer needs to refresh.
    """
    new = (player[0] + dx, player[1] + dy)
    if new in walls:
        return None
    if new in boxes:
        beyond = (new[0] + dx, new[1] + dy)
        if beyond in walls or beyond in boxes:
            return None
        # Push. Build a fresh set so undo snapshots stay independent.
        new_boxes = (boxes - {new}) | {beyond}
        return new, new_boxes, [player, new, beyond]
    return new, boxes, [player, new]


def _play_level(kb, level_idx, char_idx):
    """Run one level. Returns ("won" or "exit", moves_count)."""
    level = _LEVELS[level_idx]
    walls, goals, boxes, player = _parse_level(level)
    level_w = max(len(r) for r in level)
    level_h = len(level)
    _set_offsets(level_w, level_h)

    # Undo history: snapshots of (player, boxes) BEFORE each successful
    # move. Bounded so a long run can't OOM MicroPython (we have ~50 KB
    # of free RAM on this build; 200 frames of small tuples is nothing).
    history = []
    moves = 0

    _draw_chrome(level_idx, moves)
    _draw_level(walls, goals, boxes, player, char_idx, level_w, level_h)

    while True:
        kb.tick()
        i = _intent(kb.get_key())
        delta = None
        if i == "up":
            delta = (0, -1)
        elif i == "down":
            delta = (0, 1)
        elif i == "left":
            delta = (-1, 0)
        elif i == "right":
            delta = (1, 0)
        elif i == "exit":
            return "exit", moves
        elif i == "restart":
            walls, goals, boxes, player = _parse_level(level)
            del history[:]
            moves = 0
            _draw_chrome(level_idx, moves)
            _draw_level(walls, goals, boxes, player, char_idx, level_w, level_h)
        elif i == "undo" and history:
            # Restore the snapshot. Repaint the whole level — cheaper
            # than tracking the exact dirty set for an undo, and the
            # level fits in <200 cells so it's plenty fast.
            player, boxes = history.pop()
            if moves > 0:
                moves -= 1
            _update_moves(moves)
            _draw_level(walls, goals, boxes, player, char_idx, level_w, level_h)

        if delta is not None:
            r = _try_move(delta[0], delta[1], walls, boxes, player)
            if r is not None:
                # Snapshot BEFORE applying. set(boxes) defensively copies
                # so a later mutation of `boxes` can't reach back into
                # this snapshot.
                history.append((player, set(boxes)))
                if len(history) > 200:
                    del history[0]
                player, boxes, dirty = r
                moves += 1
                _update_moves(moves)
                for cx, cy in dirty:
                    _redraw_cell(cx, cy, walls, goals, boxes, player, char_idx)
                if boxes == goals:
                    return "won", moves

        time.sleep_ms(40)


def _level_complete_screen(kb, level_idx, moves):
    """Blocking between-level screen. Returns 'next' / 'restart' / 'exit'."""
    _LCD.fillRect(0, 21, _W, _H - 21, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _BLACK)
    t = "Level {} solved!".format(level_idx + 1)
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 36)
    _LCD.setTextColor(_CREAM, _BLACK)
    s = "moves: {}".format(moves)
    _LCD.drawString(s, (_W - _LCD.textWidth(s)) // 2, 60)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "Enter next   R replay   Q exit"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 90)

    while True:
        kb.tick()
        i = _intent(kb.get_key())
        if i == "advance":
            return "next"
        if i == "restart":
            return "restart"
        if i == "exit":
            return "exit"
        time.sleep_ms(40)


def _all_complete_screen(kb, total_moves):
    """Shown after the last level. Returns 'restart' or 'exit'."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Sokoban  done", 6, 5)
    _LCD.setTextColor(_ORANGE, _BLACK)
    t = "All levels solved!"
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 44)
    _LCD.setTextColor(_CREAM, _BLACK)
    s = "total moves: {}".format(total_moves)
    _LCD.drawString(s, (_W - _LCD.textWidth(s)) // 2, 66)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "Enter again   Q exit"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 92)
    while True:
        kb.tick()
        i = _intent(kb.get_key())
        if i in ("restart", "advance"):
            return "restart"
        if i == "exit":
            return "exit"
        time.sleep_ms(40)


# ---- character picker. Shows a scaled-up preview of the highlighted
# sprite on the left, the name and a position-dot row on the right.

# 72x72 portrait box for the picker. Same footprint as the launcher's
# burst animation so the picker reads visually as "part of the suite".
# Each portrait is hand-composed (not a scale-up of the in-game tile)
# — at this size we can afford faces, hair shape and gender-specific
# accessories, which is the whole point of the picker screen.
_PREVIEW_SIZE = 72


def _draw_preview(char_idx, px, py):
    # Clear the preview box once; each portrait function then layers
    # background -> body -> face -> hair -> headpiece -> features.
    # The ordering matters; comments inside each function spell it out.
    _LCD.fillRect(px, py, _PREVIEW_SIZE, _PREVIEW_SIZE, _BLACK)
    if char_idx == 0:
        _draw_preview_king(px, py)
    elif char_idx == 1:
        _draw_preview_queen(px, py)
    elif char_idx == 2:
        _draw_preview_prince(px, py)
    else:
        _draw_preview_princess(px, py)


def _draw_preview_king(px, py):
    # Body: orange robe with a cream collar and bottom trim.
    _LCD.fillRect(px + 4, py + 46, 64, 26, _ORANGE)
    _LCD.fillRect(px + 20, py + 46, 32, 8, _CREAM)
    _LCD.fillRect(px + 4, py + 68, 64, 4, _CREAM)
    # Face + side hair tufts (light hair, mature king).
    _LCD.fillRect(px + 16, py + 18, 40, 28, _SKIN)
    _LCD.fillRect(px + 14, py + 18, 4, 14, _CREAM)
    _LCD.fillRect(px + 54, py + 18, 4, 14, _CREAM)
    # Crown over forehead — band + three spikes + red jewel.
    _LCD.fillRect(px + 12, py + 12, 48, 8, _ORANGE)
    _LCD.fillRect(px + 14, py + 4, 6, 10, _ORANGE)
    _LCD.fillRect(px + 33, py + 0, 6, 14, _ORANGE)
    _LCD.fillRect(px + 52, py + 4, 6, 10, _ORANGE)
    _LCD.fillRect(px + 32, py + 14, 8, 4, _RED)
    # Eyes.
    _LCD.fillRect(px + 24, py + 26, 5, 5, _BLACK)
    _LCD.fillRect(px + 43, py + 26, 5, 5, _BLACK)
    # Mustache + beard — the male cue at portrait size.
    _LCD.fillRect(px + 22, py + 36, 28, 3, _CREAM)
    _LCD.fillRect(px + 26, py + 39, 20, 7, _CREAM)


def _draw_preview_queen(px, py):
    # Long dark hair frames the face and spills onto the shoulders.
    _LCD.fillRect(px + 8, py + 8, 56, 50, _DARK)
    # Dress (orange) with cream belt + trim.
    _LCD.fillRect(px + 6, py + 48, 60, 24, _ORANGE)
    _LCD.fillRect(px + 6, py + 48, 60, 4, _CREAM)
    _LCD.fillRect(px + 6, py + 68, 60, 4, _CREAM)
    # Face (narrower than the king's so the hair frame reads as long).
    _LCD.fillRect(px + 20, py + 18, 32, 26, _SKIN)
    # Crown — slightly smaller than the king's (queen consort scale),
    # still three spikes so it reads as royalty rather than a tiara.
    _LCD.fillRect(px + 18, py + 10, 36, 6, _ORANGE)
    _LCD.fillRect(px + 20, py + 4, 4, 8, _ORANGE)
    _LCD.fillRect(px + 34, py + 2, 4, 12, _ORANGE)
    _LCD.fillRect(px + 48, py + 4, 4, 8, _ORANGE)
    _LCD.fillRect(px + 33, py + 12, 6, 4, _RED)
    # Eyes + lips (the lips are the gender cue paired with no beard).
    _LCD.fillRect(px + 25, py + 24, 4, 4, _BLACK)
    _LCD.fillRect(px + 43, py + 24, 4, 4, _BLACK)
    _LCD.fillRect(px + 31, py + 34, 10, 3, _RED)


def _draw_preview_prince(px, py):
    # Cape behind (orange) with a cream coat in front and orange trim.
    _LCD.fillRect(px + 2, py + 44, 68, 28, _ORANGE)
    _LCD.fillRect(px + 12, py + 44, 48, 28, _CREAM)
    _LCD.fillRect(px + 12, py + 44, 48, 4, _ORANGE)
    _LCD.fillRect(px + 12, py + 62, 48, 4, _ORANGE)
    # Face.
    _LCD.fillRect(px + 18, py + 18, 36, 26, _SKIN)
    # Hair — short, dark, with a bangs strip dropping over the brow.
    _LCD.fillRect(px + 16, py + 8, 40, 12, _DARK)
    _LCD.fillRect(px + 18, py + 16, 18, 4, _DARK)
    # Eyes + small smile (no beard — keeps the "young prince" read).
    _LCD.fillRect(px + 25, py + 26, 4, 4, _BLACK)
    _LCD.fillRect(px + 43, py + 26, 4, 4, _BLACK)
    _LCD.fillRect(px + 28, py + 36, 14, 3, _RED)


def _draw_preview_princess(px, py):
    # Long blond hair (the female cue, frames the face top + sides).
    _LCD.fillRect(px + 8, py + 8, 56, 50, _CREAM)
    # Dress (orange) with cream belt + bottom trim.
    _LCD.fillRect(px + 8, py + 48, 56, 24, _ORANGE)
    _LCD.fillRect(px + 8, py + 48, 56, 3, _CREAM)
    _LCD.fillRect(px + 8, py + 68, 56, 4, _CREAM)
    # Face (smallest of the four — fits the young-royal silhouette).
    _LCD.fillRect(px + 22, py + 18, 28, 26, _SKIN)
    # Tiara: a narrow orange band with a center jewel — distinct from
    # the queen's three-spike crown.
    _LCD.fillRect(px + 22, py + 10, 28, 4, _ORANGE)
    _LCD.fillRect(px + 33, py + 6, 6, 8, _ORANGE)
    _LCD.fillRect(px + 34, py + 8, 4, 4, _RED)
    # Eyes + cheek blush dots + small red lips.
    _LCD.fillRect(px + 27, py + 22, 4, 4, _BLACK)
    _LCD.fillRect(px + 41, py + 22, 4, 4, _BLACK)
    _LCD.fillRect(px + 24, py + 30, 3, 3, _ORANGE)
    _LCD.fillRect(px + 45, py + 30, 3, 3, _ORANGE)
    _LCD.fillRect(px + 31, py + 36, 10, 3, _RED)


def _draw_character_select(cursor):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Sokoban  pick hero", 6, 5)

    _draw_preview(cursor, 20, 30)

    name = _CHARACTERS[cursor]
    _LCD.setTextSize(2)
    _LCD.setTextColor(_CREAM, _BLACK)
    # Right-column anchor at x=110 lines up with the preview's right
    # edge plus a comfortable gap.
    _LCD.drawString(name, 112, 40)
    _LCD.setTextSize(1)

    # Position dots — selected is ORANGE, others GRAY_MID. 4 dots
    # spaced 14 px apart fits in the right column without wrapping.
    dx = 112
    dy = 72
    for i in range(len(_CHARACTERS)):
        color = _ORANGE if i == cursor else _GRAY_MID
        _LCD.fillRect(dx + i * 14, dy, 8, 8, color)

    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "A D scroll   Enter start"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _choose_character(kb):
    """Blocking. Returns the chosen character index, or None if Q/Esc."""
    cursor = 0
    _draw_character_select(cursor)
    while True:
        kb.tick()
        i = _intent(kb.get_key())
        if i == "left":
            cursor = (cursor - 1) % len(_CHARACTERS)
            _draw_character_select(cursor)
        elif i == "right":
            cursor = (cursor + 1) % len(_CHARACTERS)
            _draw_character_select(cursor)
        elif i == "advance":
            return cursor
        elif i == "exit":
            return None
        time.sleep_ms(40)


def run():
    _set_font()
    kb = MatrixKeyboard()
    # Debounce the keypress that launched us from App List so it
    # doesn't immediately register as a character-pick advance.
    time.sleep_ms(400)
    try:
        # Outer loop allows "play again" from the all-done screen to
        # bounce back into the character picker.
        while True:
            char_idx = _choose_character(kb)
            if char_idx is None:
                return

            total_moves = 0
            level_idx = 0
            while level_idx < len(_LEVELS):
                result, moves = _play_level(kb, level_idx, char_idx)
                if result == "exit":
                    return
                # result == "won". On the final level, skip the between-
                # level screen and roll straight into the all-done summary.
                if level_idx == len(_LEVELS) - 1:
                    total_moves += moves
                    break
                choice = _level_complete_screen(kb, level_idx, moves)
                if choice == "exit":
                    return
                if choice == "restart":
                    # Replay current level — don't bank this attempt's
                    # moves toward the run total.
                    continue
                # choice == "next"
                total_moves += moves
                level_idx += 1

            choice = _all_complete_screen(kb, total_moves)
            if choice == "exit":
                return
            # choice == "restart": fall through to the outer while, which
            # bounces back into _choose_character for a fresh run.
    finally:
        # Mirror snake / claude_buddy's exit protocol: blank the screen
        # before the soft reset so the launcher doesn't briefly flash
        # the last frame of the prior app.
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("sokoban: clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


# UIFlow's App List invokes apps both as __main__ and via import.
# Empirically the behavior is always-run, so call run() bare.
run()
