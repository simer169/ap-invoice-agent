"""Static catalog for the fake railcar-parts suppliers. ALL data is invented."""

from typing import NamedTuple


class Part(NamedTuple):
    part_no: str
    description: str
    base_price: float  # CAD list price; vendors vary around it


class Category(NamedTuple):
    key: str
    domain_word: str  # used in reserved *.example email domains
    suffixes: tuple[str, ...]  # appended to an invented surname for the vendor name
    qty_range: tuple[int, int]  # min must be >= 2 (receiving logic splits quantities)
    parts: tuple[Part, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        "brake shoes",
        "brakeparts",
        ("Brake Products", "Friction Systems", "Braking Supply"),
        (20, 200),
        (
            Part("BRK-4410", "Composition brake shoe, high-friction", 38.50),
            Part("BRK-4412", "Composition brake shoe, low-friction", 36.00),
            Part("BRK-4420", "Brake head, cast", 92.00),
            Part("BRK-4425", "Brake shoe key", 11.75),
            Part("BRK-4431", "Brake beam, truck-mounted", 410.00),
            Part("BRK-4440", "Brake hanger assembly", 64.00),
            Part("BRK-4452", "Slack adjuster", 285.00),
            Part("BRK-4460", "Brake cylinder piston", 147.00),
        ),
    ),
    Category(
        "couplers",
        "couplers",
        ("Coupler Works", "Draft Systems", "Coupling Industries"),
        (2, 20),
        (
            Part("CPL-7100", "Type E coupler body", 1850.00),
            Part("CPL-7105", "Type F coupler body", 2150.00),
            Part("CPL-7112", "Coupler knuckle", 420.00),
            Part("CPL-7118", "Coupler yoke", 760.00),
            Part("CPL-7121", "Draft gear, cushioning", 1380.00),
            Part("CPL-7130", "Coupler pin", 55.00),
            Part("CPL-7136", "Coupler carrier plate", 240.00),
            Part("CPL-7142", "Uncoupling lever assembly", 195.00),
        ),
    ),
    Category(
        "bearings",
        "bearings",
        ("Bearing Co", "Roller Bearings", "Precision Bearings"),
        (10, 80),
        (
            Part("BRG-3301", "Tapered roller bearing, class K", 310.00),
            Part("BRG-3305", "Tapered roller bearing, class F", 275.00),
            Part("BRG-3310", "Bearing adapter", 128.00),
            Part("BRG-3314", "Bearing seal, rear", 34.00),
            Part("BRG-3318", "Bearing end cap", 21.00),
            Part("BRG-3322", "Bearing retainer ring", 17.50),
            Part("BRG-3327", "Lubricant cartridge", 26.00),
            Part("BRG-3333", "Bearing wear sleeve", 62.00),
        ),
    ),
    Category(
        "valves",
        "valves",
        ("Valve & Fittings", "Pneumatic Controls", "Valve Works"),
        (5, 60),
        (
            Part("VLV-5200", "Angle cock 1.25in", 112.00),
            Part("VLV-5204", "Retainer valve", 76.00),
            Part("VLV-5210", "Control valve, freight", 690.00),
            Part("VLV-5215", "Relief valve", 98.00),
            Part("VLV-5221", "Check valve", 58.00),
            Part("VLV-5226", "Drain valve", 44.00),
            Part("VLV-5230", "Cut-out cock", 83.00),
            Part("VLV-5238", "Pressure regulating valve", 245.00),
        ),
    ),
    Category(
        "wheels",
        "wheelset",
        ("Wheel & Axle", "Wheelset Foundry", "Rolling Stock Wheels"),
        (4, 40),
        (
            Part("WHL-6010", "Cast steel wheel 36in", 780.00),
            Part("WHL-6012", "Cast steel wheel 33in", 720.00),
            Part("WHL-6020", "Forged wheel 36in", 1150.00),
            Part("WHL-6025", "Wheelset, mounted", 3250.00),
            Part("WHL-6031", "Axle, class F", 1480.00),
            Part("WHL-6037", "Journal box", 365.00),
            Part("WHL-6044", "Truck bolster spring", 54.00),
            Part("WHL-6050", "Wheel tread gauge", 85.00),
        ),
    ),
    Category(
        "paint",
        "coatings",
        ("Industrial Coatings", "Paint & Marking", "Protective Coatings"),
        (10, 100),
        (
            Part("PNT-8100", "Industrial enamel, black 20L", 142.00),
            Part("PNT-8104", "Industrial enamel, red 20L", 148.00),
            Part("PNT-8110", "Zinc-rich primer 20L", 188.00),
            Part("PNT-8115", "Reflective stripe tape roll", 96.00),
            Part("PNT-8120", "Thinner 20L", 74.00),
            Part("PNT-8126", "Stencil kit, reporting marks", 120.00),
            Part("PNT-8131", "Epoxy topcoat 20L", 215.00),
            Part("PNT-8138", "Rust converter 4L", 52.00),
        ),
    ),
    Category(
        "safety gear",
        "safetygear",
        ("Safety Supply", "Protective Equipment", "Safety Outfitters"),
        (20, 300),
        (
            Part("SFT-9001", "Hi-vis vest, class 3", 24.00),
            Part("SFT-9005", "Hard hat", 31.00),
            Part("SFT-9010", "Safety glasses", 9.50),
            Part("SFT-9014", "Steel-toe boots", 165.00),
            Part("SFT-9020", "Hearing protection earmuffs", 28.00),
            Part("SFT-9026", "Cut-resistant gloves", 17.00),
            Part("SFT-9033", "Fall arrest harness", 189.00),
            Part("SFT-9040", "Lockout/tagout kit", 78.00),
        ),
    ),
)

# Vendors per category (sums to 25), in CATEGORIES order.
VENDORS_PER_CATEGORY: tuple[int, ...] = (4, 4, 4, 4, 3, 3, 3)
