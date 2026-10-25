"""VAYLT hub case for a Raspberry Pi 4 Model B with a top fan.

Board numbers come from the official Raspberry Pi 4B mechanical drawing.
Coordinates: board origin is its corner nearest the USB C port, x along the
long edge toward USB and Ethernet, y toward the GPIO header.
"""
import cadquery as cq

# Pi 4B board
BL, BW, BT = 85.0, 56.0, 1.4
HOLES = [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)]

# Case
WALL, FLOOR, GAP = 2.0, 2.0, 1.0          # GAP = space around the board
STANDOFF_H, STANDOFF_D, SCREW_D = 4.0, 6.0, 2.5
H = 32.0                                   # outer height of the base
IL, IW = BL + 2 * GAP, BW + 2 * GAP        # inner size
OL, OW = IL + 2 * WALL, IW + 2 * WALL      # outer size
BX, BY = WALL + GAP, WALL + GAP            # board origin inside the case
BZ = FLOOR + STANDOFF_H                    # board bottom
BTOP = BZ + BT                             # board top

# Fan: works with a 30 mm fan (24 mm holes) or a 40 mm fan (32 mm holes)
FAN_C = (30.0, 26.0)                       # over the SoC, clear of the GPIO header
FAN_GRILL_D = 37.0

LID_T, LIP_H, LIP_T, FIT = 2.0, 3.0, 1.6, 0.3


def box(x0, y0, z0, dx, dy, dz):
    return cq.Workplane("XY").box(dx, dy, dz, centered=False).translate((x0, y0, z0))


def base():
    b = cq.Workplane("XY").box(OL, OW, H, centered=False).edges("|Z").fillet(3)
    b = b.cut(box(WALL, WALL, FLOOR, IL, IW, H))
    for hx, hy in HOLES:
        post = (cq.Workplane("XY").circle(STANDOFF_D / 2).circle(SCREW_D / 2)
                .extrude(STANDOFF_H).translate((BX + hx, BY + hy, FLOOR)))
        b = b.union(post)
        # screw hole through the floor too, so screws can go in from below
        b = b.cut(cq.Workplane("XY").circle(1.5).extrude(FLOOR).translate((BX + hx, BY + hy, 0)))

    # USB C, two micro HDMI, audio: one long window on the y = 0 wall
    b = b.cut(box(BX + 2.0, -1, BTOP - 1.5, 57.0, WALL + 2, 10.5))
    # 2x USB 2, 2x USB 3, Ethernet on the far short wall: open at the top so the board drops in
    b = b.cut(box(OL - WALL - 1, BY + 0.5, BTOP - 1.5, WALL + 2, BW - 1.0, H))
    # microSD slot on the near short wall, under the board
    b = b.cut(box(-1, BY + 28.0 - 8.0, FLOOR + 0.5, WALL + 2, 16.0, BZ - FLOOR + 2.5))
    # GPIO side wall: vent slots for the fan exhaust
    for i in range(6):
        b = b.cut(box(BX + 10 + i * 8, OW - WALL - 1, BTOP + 4, 4.0, WALL + 2, 14.0))
    # floor vents under the board
    for i in range(7):
        b = b.cut(box(BX + 12 + i * 7, BY + 14, -1, 3.5, 28.0, FLOOR + 2))
    # key notch so the lid only fits one way (fan over the SoC)
    b = b.cut(box(-1, OW / 2 - 5, H - LIP_H, WALL + 2, 10.0, LIP_H + 1))
    return b


def lid():
    l = cq.Workplane("XY").box(OL, OW, LID_T, centered=False).edges("|Z").fillet(3)
    # lip that sits inside the walls
    lx, ly = IL - 2 * FIT, IW - 2 * FIT
    lip = box(WALL + FIT, WALL + FIT, -LIP_H, lx, ly, LIP_H)
    lip = lip.cut(box(WALL + FIT + LIP_T, WALL + FIT + LIP_T, -LIP_H - 1, lx - 2 * LIP_T, ly - 2 * LIP_T, LIP_H + 2))
    # the right side has the open port slot, so cut the lip back there
    lip = lip.cut(box(OL - WALL - 3, 0, -LIP_H - 1, 10, OW, LIP_H + 1))
    l = l.union(lip)
    # key tab that drops into the notch
    l = l.union(box(-0.0, OW / 2 - 5 + FIT, -LIP_H + 0.2, WALL + FIT, 10.0 - 2 * FIT, LIP_H - 0.2))
    # fan grill: parallel slots inside a circle
    fx, fy = BX + FAN_C[0], BY + FAN_C[1]
    disc = cq.Workplane("XY").circle(FAN_GRILL_D / 2).extrude(LID_T + 2).translate((fx, fy, -1))
    slots = None
    for i in range(-4, 5):
        s = box(fx - 20, fy + i * 4.2 - 1.4, -1, 40, 2.8, LID_T + 2)
        slots = s if slots is None else slots.union(s)
    l = l.cut(disc.intersect(slots))
    # fan screw holes for both 30 mm and 40 mm fans
    for half in (12.0, 16.0):
        for sx in (-1, 1):
            for sy in (-1, 1):
                l = l.cut(cq.Workplane("XY").circle(1.6).extrude(LID_T + 2)
                          .translate((fx + sx * half, fy + sy * half, -1)))
    # small vents over the USB and Ethernet side
    for i in range(4):
        l = l.cut(box(BX + 66, BY + 8 + i * 11, -1, 14.0, 3.0, LID_T + 2))
    return l


if __name__ == "__main__":
    b, l = base(), lid()
    # lid printed upside down: flat top on the bed
    l_print = l.rotate((0, 0, 0), (1, 0, 0), 180).translate((0, OW, LID_T))
    cq.exporters.export(b, "VAYLT_Pi4_Case_Base.stl", tolerance=0.02, angularTolerance=0.1)
    cq.exporters.export(l_print, "VAYLT_Pi4_Case_Lid.stl", tolerance=0.02, angularTolerance=0.1)
    cq.exporters.export(b, "VAYLT_Pi4_Case_Base.step")
    cq.exporters.export(l, "VAYLT_Pi4_Case_Lid.step")
    asm = b.union(l.translate((0, 0, H)))
    cq.exporters.export(asm, "assembled_preview.stl")
    print("outer", OL, OW, H, "board top z", BTOP, "lid underside z", H)
