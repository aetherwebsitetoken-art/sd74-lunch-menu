#!/usr/bin/env python3
"""
Lincolnwood SD74 lunch menu fetcher
====================================

Pulls the school lunch (and optionally breakfast) menus from MealViewer and
writes them to menu.json, then refreshes the offline copy baked into
index.html so the page is never blank.

Where the data comes from
--------------------------
schools.mealviewer.com is a JavaScript app -- its page HTML is an empty
shell, so there's nothing useful to scrape from it. The app is fed by a
public, unauthenticated JSON API:

    https://api.mealviewer.com/api/v4/school/<schoolKey>/<MM-DD-YYYY>/<MM-DD-YYYY>/

This script uses that endpoint directly. JSON with explicit dates is far more
reliable than scraping rendered text.

A note on parsing
------------------
MealViewer's response shape is undocumented and has varied between districts
and versions, so the extractor below walks the structure by *looking for*
date fields and food-item fields rather than assuming one fixed set of key
names. If it still can't find anything, it prints a structural dump of what
it actually received so the problem can be diagnosed from the log instead of
guessed at.

Run locally:
    pip install requests
    python3 fetch_menu.py
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

# --- Configuration ---------------------------------------------------------

# The key in the MealViewer URL. From
# schools.mealviewer.com/school/LincolnwoodSchoolDistrict74
# If the district later splits menus per building, add those keys here and
# each will appear as its own tab on the page.
SCHOOL_KEYS = ["LincolnwoodSchoolDistrict74"]

API = "https://api.mealviewer.com/api/v4/school/{key}/{start}/{end}/"

DAYS_BEHIND = 7      # keep a little history so "today" always has context
DAYS_AHEAD = 70      # far enough to cover all of next month

# Meal periods to keep, in display order. Add "Breakfast" if wanted.
MEALS = ["Lunch", "Breakfast"]

# Food categories worth showing, in order. Anything not listed still appears,
# just after these. Condiments are dropped -- they're noise on a menu board.
TYPE_ORDER = ["Entree", "Entrees", "Main", "Side", "Sides", "Vegetable",
              "Vegetables", "Fruit", "Fruits", "Grain", "Milk"]
SKIP_TYPES = {"condiment", "condiments"}

HEADERS = {
    "User-Agent": "SD74LunchMenuBot/1.0 (parent-run menu sync)",
    "Accept": "application/json",
}
TIMEOUT = 25
ATTEMPTS = 3
RETRY_WAIT = 2

# index.html is the TEMPLATE (what gets updated when the design changes).
# embed.html is what goes into Google Sites. Only this script ever writes
# embed.html, so the copy that gets pasted is always filled in -- a new
# template being uploaded can never leave Google Sites with a blank page.
# Each (template, output) pair: the bot fills the live link and a saved
# copy into the template and writes the paste-ready file.
PAGES = [
    ("index.html", "embed.html"),        # full menu: week + month views
    ("week.html",  "week-embed.html"),   # compact: this week only
]
URL_REGION_RE = re.compile(r'(/\*URL_START\*/).*?(/\*URL_END\*/)', re.DOTALL)
FALLBACK_REGION_RE = re.compile(r'(/\*FALLBACK_START\*/).*?(/\*FALLBACK_END\*/)', re.DOTALL)


# --- Networking ------------------------------------------------------------

def fetch_json(url):
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < ATTEMPTS:
                print(f"    (attempt {attempt} failed: {e} -- retrying)", file=sys.stderr)
                time.sleep(RETRY_WAIT)
    raise last


# --- Tolerant extraction ---------------------------------------------------

DATE_KEYS = ("dateFull", "date_full", "date", "menuDate", "servingDate")
NAME_KEYS = ("item_Name", "itemName", "name", "menuItemDescription", "description")
# The subset that only a menu item ever carries -- never a line or a block.
ITEM_NAME_KEYS = ("item_Name", "itemName", "menuItemDescription", "foodItemName")
TYPE_KEYS = ("item_Type", "itemType", "type", "category", "menuCategory",
             # MealViewer's own field name -- without this the categories
             # were being ignored entirely and every item looked untyped.
             "foodCategoryName", "food_category_name", "categoryName",
             "itemCategory", "productCategoryName")
BLOCK_NAME_KEYS = ("blockName", "block_Name", "mealPeriod", "name", "menuBlockName")

DATE_PATTERNS = (
    "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y",
    "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S",
)


def first_key(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def parse_any_date(value):
    if not isinstance(value, str):
        return None
    v = value.strip()
    for p in DATE_PATTERNS:
        try:
            return datetime.strptime(v[:len(datetime.now().strftime(p)) + 4], p).date()
        except ValueError:
            continue
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', v)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', v)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def iter_dicts(node):
    """Every dict anywhere inside a nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_dicts(v)


# Nutrition facts and allergen entries are shaped just like food items --
# they're objects with a "name" -- so they have to be excluded deliberately
# or the menu fills up with "Calcium (mg)", "Wheat", "No Known Allergens".

# 1. Never descend into a branch whose key says it holds nutrition/allergens.
# Dietary tags ("Gluten-free", "Lacto-ovo Vegetarian") arrive as a little
# list INSIDE the dish they describe. Skipping those branches does two jobs:
# the tags never get collected as if they were food, and the dish carrying
# them still counts as a single item -- otherwise "Chicken Nuggets" was
# thrown out for having tags, and its tag headlined the day instead.
SKIP_BRANCH_RE = re.compile(
    r'nutrient|nutrition|allergen|vitamin|mineral|'
    r'trait|diet|icon|badge|attribute|restriction|certification|preference',
    re.IGNORECASE)

# 2. Reject names that are obviously a measurement or an allergen label.
# Matched against the WHOLE name (a label, optionally with an amount), so a
# dish that merely starts with one of these words survives. It used to match
# just the start, which quietly deleted "Protein Box Option" from every day
# it was on -- mistaken for a "Protein: 12g" row.
_NUTRIENT_WORDS = (
    r'(?:calories|calorie|protein|sodium|sugars?|added\s+sugars?|fiber|fibre|'
    r'dietary\s+fiber|cholesterol|potassium|calcium|iron|carbohydrates?|'
    r'total\s+carbs?|total\s+fat|sat\.?\s*fat|saturated\s+fat|trans\s*fat|'
    r'vitamin\s+\w+|serving\s+size)')
NUTRIENT_NAME_RE = re.compile(
    r'^\s*' + _NUTRIENT_WORDS +
    r'\s*(?:[:\-]?\s*[\d.,]+\s*(?:mg|g|iu|mcg|kcal|cal|%)?)?'
    r'\s*(?:\((?:mg|g|iu|mcg|kcal|%)\))?\s*$'
    r'|\((?:mg|g|iu|mcg|kcal)\)\s*$', re.IGNORECASE)

ALLERGEN_NAME_RE = re.compile(
    r'^\s*(no known allergens|contains|may contain|wheat|soy|soybeans?|milk|egg|eggs|'
    r'peanuts?|tree\s*nuts?|fish|shellfish|sesame|gluten|dairy)\s*$', re.IGNORECASE)


# 3. Labels that leak through with no category attached: day names, meal
#    periods, the line or school name, placeholders. Used only to judge
#    untyped leaves, which is where this junk shows up.
META_NAME_RE = re.compile(r"""^\s*(?:
      (?:mon|tues|wednes|thurs|fri|satur|sun)day
    | january|february|march|april|may|june|july|august
    | september|october|november|december
    | breakfast|brunch|lunch|dinner|supper|snack|a\s*la\s*carte
    | menu|menus|line|lines|station|cafeteria|kitchen|caf[eé]
    | school|district|elementary|middle|high|grade|campus
    | today|tomorrow|week|day|date|notes?|info|information
    | choice|choices|select|selection|offered|available|served
    | n/?a|none|tbd|closed|no\s+school
    )\s*$""", re.IGNORECASE | re.VERBOSE)


def looks_like_dish(name):
    """A stricter test, for items that arrived with no category at all."""
    n = (name or "").strip()
    if len(n) < 3 or len(n) > 60:
        return False
    if not re.search(r'[A-Za-z]{3}', n):          # needs actual words
        return False
    if META_NAME_RE.match(n):
        return False
    # "Cafeteria Line", "Grill Station": the name of a place, not a dish.
    # ("Salad Bar" and "Potato Bar" are dishes, so "bar" stays out of this.)
    if re.search(r'\b(?:line|lines|station|stations|menu|kitchen|counter)\s*$', n, re.I):
        return False
    if NUTRIENT_NAME_RE.search(n) or ALLERGEN_NAME_RE.match(n):
        return False
    return True


# A name that is only a dietary label. Matched against the WHOLE name, so
# "Vegetarian Chili" or "Burger Plant Based" are still food.
DIET_NAME_RE = re.compile(r"""^\s*(?:
      gluten[\s-]*free | dairy[\s-]*free | nut[\s-]*free | peanut[\s-]*free
    | egg[\s-]*free | soy[\s-]*free | allergen[\s-]*free
    | (?:lacto[\s-]*)?(?:ovo[\s-]*)?vegetarian | lacto[\s-]*ovo | vegan
    | plant[\s-]*based | halal | kosher | organic | local(?:ly\s+grown)?
    | whole[\s-]*grain(?:[\s-]*rich)? | low[\s-]*(?:fat|sodium|sugar)
    | reduced[\s-]*(?:fat|sodium) | contains\s+\w+ | spicy | new\s+item
    )\s*$""", re.IGNORECASE | re.VERBOSE)


def looks_like_food(name, itype):
    """Filter out nutrition/allergen rows that share the food-item shape."""
    if NUTRIENT_NAME_RE.search(name):
        return False
    if DIET_NAME_RE.match(name):            # a tag, never a dish
        return False
    # "Milk" and "Egg" are real allergen labels but also real menu items --
    # so only reject them when they arrive with no food category attached.
    if not itype and ALLERGEN_NAME_RE.match(name):
        return False
    if itype.lower() in SKIP_TYPES:
        return False
    return True


def collect_items(node, _key_path=""):
    """Food items under this node, skipping nutrition and allergen branches."""
    out = []

    def walk(n, key_name=""):
        if SKIP_BRANCH_RE.search(key_name or ""):
            return                          # don't descend at all
        if isinstance(n, dict):
            name = first_key(n, NAME_KEYS)
            itype = first_key(n, TYPE_KEYS)
            itype = itype.strip() if isinstance(itype, str) else ""
            if isinstance(name, str):
                nm = name.strip()
                # A field that only ever names a menu item ("itemName"), as
                # opposed to a generic "name" that a line or block also has.
                item_key = any(k in n for k in ITEM_NAME_KEYS)
                has_children = any(
                    isinstance(v, (list, dict)) and v
                    for k, v in n.items() if not SKIP_BRANCH_RE.search(k))
                # A dish named by an item field IS a dish, whatever is nested
                # inside it. Insisting on "nothing nested" is what threw out
                # "Chicken Nuggets": MealViewer hangs its dietary tags off the
                # dish, so a tagged dish looked like a container -- and every
                # tagged dish vanished, leaving its tags behind as "food".
                # Generic names still need to be leaves, so a cafeteria line
                # called "Main Line" is never mistaken for a dish.
                is_item = item_key or not has_children
                if nm and len(nm) <= 120 and is_item and looks_like_food(nm, itype):
                    out.append({"name": nm, "type": itype})
                if item_key:
                    # Don't go looking for more food inside a dish: all that
                    # lives there is its tags, icons and nutrition.
                    return
            for k, v in n.items():
                walk(v, k)
        elif isinstance(n, list):
            for v in n:
                walk(v, key_name)

    walk(node, _key_path)

    # Anything with a real category is trusted. Untyped leaves are usually
    # stray metadata -- but NOT always: the kitchen regularly files the day's
    # actual dish with no category, and quietly dropping it is how "Bagel
    # Option" ends up headlining a chicken-nugget day. So keep an untyped
    # leaf when it reads like a dish and isn't repeating a typed one.
    typed = [it for it in out if it["type"]]
    if typed:
        have = {it["name"].strip().lower() for it in typed}
        out = typed + [it for it in out
                       if not it["type"]
                       and it["name"].strip().lower() not in have
                       and looks_like_dish(it["name"])]

    seen, uniq = set(), []
    for it in out:
        k = it["name"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq


def type_rank(itype):
    """Position of a food category in TYPE_ORDER, matching loosely so
    variants like "Main Dish" or "Entrees" rank with "Entree"."""
    t = (itype or "").strip().lower()
    if not t:
        return len(TYPE_ORDER)
    for i, known in enumerate(TYPE_ORDER):
        k = known.lower()
        if t == k or t.startswith(k) or k.startswith(t):
            return i
    if re.search(r'entr|main|hot\s*food|featured', t):
        return 0          # any other way of saying "entree" still leads
    return len(TYPE_ORDER)


def sort_items(items):
    return sorted(items, key=lambda it: (type_rank(it["type"]), it["name"].lower()))


def extract_days(payload):
    """{ 'YYYY-MM-DD': { 'Lunch': [items], ... } } from a MealViewer response."""
    days = {}

    # Prefer an explicit schedule list if one is present; otherwise scan the
    # whole payload for dict nodes that carry a date.
    candidates = None
    for k in ("menuSchedules", "menuSchedule", "menus", "days"):
        v = payload.get(k) if isinstance(payload, dict) else None
        if isinstance(v, list) and v:
            candidates = v
            break
    if candidates is None:
        candidates = [d for d in iter_dicts(payload)
                      if parse_any_date(first_key(d, DATE_KEYS) or "")]

    for day_node in candidates:
        if not isinstance(day_node, dict):
            continue
        day = None
        for d in iter_dicts(day_node):
            day = parse_any_date(first_key(d, DATE_KEYS) or "")
            if day:
                break
        if not day:
            continue

        # Find meal blocks under this day, if the structure has them.
        blocks = []
        for k in ("menuBlocks", "menuBlock", "meals", "blocks"):
            v = day_node.get(k)
            if isinstance(v, list) and v:
                blocks = v
                break

        if blocks:
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bname = first_key(b, BLOCK_NAME_KEYS) or "Lunch"
                bname = str(bname).strip().title()
                if MEALS and not any(m.lower() in bname.lower() for m in MEALS):
                    continue
                items = sort_items(collect_items(b))
                if items:
                    days.setdefault(day.isoformat(), {}).setdefault(bname, []).extend(items)
        else:
            items = sort_items(collect_items(day_node))
            if items:
                days.setdefault(day.isoformat(), {}).setdefault("Lunch", []).extend(items)

    # Tidy: drop duplicate items that arrived via several paths
    for day, meals in days.items():
        for meal, items in meals.items():
            seen, uniq = set(), []
            for it in items:
                k = it["name"].lower()
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(it)
            meals[meal] = uniq
    return days


# --- Picking each day's real lunch ------------------------------------------
#
# Most menus include the same fallback choices every day ("Bagel Option",
# "Chef Salad Option"). They're categorised as entrees and would otherwise
# headline every single day. The day's actual lunch is the entree that
# CHANGES from day to day, so alternates are identified two ways:
#   - the word "option"/"alternate" in the name, and
#   - appearing on most of the days in the data.
ALT_NAME_RE = re.compile(r'\b(option|alternate|alt\.?|grab\s*(?:&|and)\s*go|daily)\b',
                         re.IGNORECASE)
RECURRING_SHARE = 0.6        # on >= 60% of days counts as a daily staple
MIN_DAYS_FOR_RECURRENCE = 3  # need a few days before recurrence means anything

# Some days the kitchen never files the real dish under an entree category --
# it arrives with a blank category, or one this script doesn't recognise
# ("Chicken Nuggets" under nothing at all). Such an item can still headline,
# but only if it isn't obviously a drink, a piece of fruit or a condiment.
# Matching is done word by word against the whole name, so "Fresh Orange" is
# ruled out while "Orange Chicken" is not.
NOT_A_MAIN_WORDS = {
    "milk", "juice", "water", "lemonade", "tea", "coffee", "cocoa", "soda",
    "fruit", "fruits", "apple", "apples", "applesauce", "banana", "bananas",
    "orange", "oranges", "mandarin", "mandarins", "grape", "grapes", "berry",
    "berries", "strawberry", "strawberries", "blueberry", "blueberries",
    "raspberry", "raspberries", "melon", "watermelon", "cantaloupe", "kiwi",
    "pear", "pears", "peach", "peaches", "pineapple", "plum", "plums",
    "apricot", "apricots", "raisin", "raisins", "craisins", "prunes",
    "vegetable", "vegetables", "veggie", "veggies", "carrot", "carrots",
    "celery", "broccoli", "cauliflower", "cucumber", "cucumbers",
    "condiment", "condiments", "ketchup", "mustard", "mayo", "mayonnaise",
    "dressing", "ranch", "syrup", "butter", "jelly", "jam", "napkin",
    "utensil", "utensils", "silverware", "straw",
}
# Words that only ever decorate a name and so don't decide anything.
NAME_MODIFIER_WORDS = {
    "fresh", "freshly", "assorted", "asst", "variety", "choice", "chilled",
    "cold", "sliced", "diced", "cut", "whole", "baby", "mini", "small",
    "large", "cup", "cups", "bowl", "side", "sides", "serving", "of", "and",
    "or", "with", "w", "the", "a", "an", "low", "reduced", "fat", "free",
    "nonfat", "skim", "unsweetened", "canned", "frozen", "seasonal", "local",
    "oz", "ct", "each", "individual", "packet", "pack", "cold", "hot",
}


def _norm(name):
    return re.sub(r'[^a-z0-9]+', '', name.lower())


def _words(name):
    return re.findall(r'[a-z]+', name.lower())


# Categories that can never be the headline, whatever the name says.
NEVER_HEADLINE_TYPE_RE = re.compile(
    r'fruit|milk|drink|bever|juice|dessert|condiment', re.IGNORECASE)

# Used only to break a tie between two candidate dishes. A starch on its own
# is probably the side; anything naming a protein or a dish form is probably
# the main. Nothing is excluded by this -- it only decides which goes first.
SIDE_HINT_WORDS = {
    "potato", "potatoes", "mash", "mashed", "fries", "tots", "tater", "tator",
    "rice", "roll", "rolls", "bread", "breadstick", "breadsticks", "biscuit",
    "muffin", "cornbread", "chips", "crackers", "beans", "corn", "peas",
    "broccoli", "carrots", "salad", "coleslaw", "slaw", "gravy", "sauce",
}
MAIN_HINT_WORDS = {
    "chicken", "beef", "pork", "turkey", "ham", "fish", "shrimp", "meatball",
    "meatballs", "nugget", "nuggets", "patty", "burger", "cheeseburger",
    "hamburger", "hotdog", "frank", "taco", "tacos", "nacho", "nachos",
    "pizza", "sandwich", "wrap", "burrito", "quesadilla", "fajita", "fajitas",
    "lasagna", "spaghetti", "alfredo", "penne", "ziti", "mac", "chili",
    "stir", "bowl", "sub", "gyro", "calzone", "pasta", "noodle", "noodles",
}


def looks_like_side(name):
    """A tiebreaker, not a filter: true when the name reads like a starch or
    vegetable and names no protein or dish of its own."""
    words = set(_words(name))
    if words & MAIN_HINT_WORDS:
        return False
    return bool(words & SIDE_HINT_WORDS)


def not_a_main(name):
    """True when every word in the name is either a modifier or something that
    is never a lunch in its own right (milk, fruit, a condiment)."""
    words = _words(name)
    if not words:
        return True
    if not any(w in NOT_A_MAIN_WORDS for w in words):
        return False
    return all(w in NOT_A_MAIN_WORDS or w in NAME_MODIFIER_WORDS for w in words)


def mark_featured(days):
    """Flag each day's headline item and its daily alternates, and reorder so
    the headline comes first and alternates last."""
    for meal in {m for d in days.values() for m in d}:
        meal_days = [d[meal] for d in days.values() if d.get(meal)]
        counts = {}
        for items in meal_days:
            for n in {_norm(i["name"]) for i in items}:
                counts[n] = counts.get(n, 0) + 1
        n_days = len(meal_days)

        side_rank = type_rank("Side")
        fruit_rank = type_rank("Fruit")
        unknown_rank = len(TYPE_ORDER)

        def share(item):
            """How much of the month this item appears on. Below the minimum
            number of days, recurrence tells us nothing, so treat it as 0."""
            if n_days < MIN_DAYS_FOR_RECURRENCE:
                return 0.0
            return counts.get(_norm(item["name"]), 0) / n_days

        def is_alt(item):
            if ALT_NAME_RE.search(item["name"]):
                return True
            # Recurrence only marks an *entree* as a daily alternate. Fruit,
            # milk and sides legitimately show up most days -- that's just
            # a normal side, not an alternative lunch.
            if type_rank(item["type"]) >= side_rank:
                return False
            return share(item) >= RECURRING_SHARE

        def could_be_the_dish(item):
            """A one-off item that reads like the day's actual lunch, whatever
            category it arrived under -- or none at all.

            This used to insist the item be uncategorised, which assumed the
            kitchen either files the real dish as an "Entree" or files it
            under nothing. It does neither reliably: put the nuggets under
            "Side" and they were invisible to both tests, so the standing
            bagel headlined the day. Judge the item, not its filing."""
            if item["alt"]:
                return False
            if not_a_main(item["name"]):          # milk, fruit, a condiment
                return False
            if NEVER_HEADLINE_TYPE_RE.search(item["type"] or ""):
                return False
            return share(item) < RECURRING_SHARE  # not an everyday staple

        for day in days.values():
            items = day.get(meal)
            if not items:
                continue
            for it in items:
                it["alt"] = is_alt(it)
                it.pop("featured", None)

            entrees = [i for i in items if type_rank(i["type"]) < side_rank]
            # Priority:
            #   1. a real (non-alternate) entree -- the normal case;
            #   2. a one-off dish under any other category, rarest first,
            #      so "Chicken Nuggets" beats the standing bagel however
            #      the kitchen happened to file it;
            #   3. any entree, alternates included -- an "option" still
            #      beats headlining a piece of fruit;
            #   4. anything non-alternate, then whatever's there.
            # Rarest first -- the dish that makes this day different. Where
            # the data gives no recurrence to go on, the name breaks the tie
            # so a starch doesn't outrank the nuggets beside it.
            loose = sorted((i for i in items if could_be_the_dish(i)),
                           key=lambda i: (share(i), looks_like_side(i["name"]),
                                          type_rank(i["type"])))
            pool = [i for i in entrees if not i["alt"]] \
                or loose \
                or entrees \
                or [i for i in items if not i["alt"]] \
                or items
            pool[0]["featured"] = True

            head = [i for i in items if i.get("featured")]
            rest = [i for i in items if not i.get("featured") and not i["alt"]]
            alts = [i for i in items if not i.get("featured") and i["alt"]]
            day[meal] = head + rest + alts
    return days


def school_name(payload):
    for key in ("schoolName", "name", "physicalLocationName"):
        for d in iter_dicts(payload):
            v = d.get(key)
            if isinstance(v, str) and v.strip() and len(v) < 90:
                return v.strip()
    return None


def dump_structure(payload, limit=60):
    """Print the shape of an unexpected response so it can be diagnosed."""
    print("--- RESPONSE STRUCTURE DUMP START ---", file=sys.stderr)
    if isinstance(payload, dict):
        print(f"top-level keys: {list(payload.keys())}", file=sys.stderr)
    text = json.dumps(payload)[:4000]
    print(f"first 4000 chars of JSON:\n{text}", file=sys.stderr)
    print("--- RESPONSE STRUCTURE DUMP END ---", file=sys.stderr)
    print("Copy everything between DUMP START and DUMP END and send it to "
          "Claude to adjust the parser.", file=sys.stderr)


# --- Keeping index.html in sync -------------------------------------------

def build_embed(menu_payload):
    """Write each paste-ready page from its template. Templates are never
    modified."""
    for template, out in PAGES:
        _build_one(template, out, menu_payload)


def _build_one(TEMPLATE_FILE, EMBED_FILE, menu_payload):
    if not os.path.exists(TEMPLATE_FILE):
        print(f"(no {TEMPLATE_FILE} -- skipping {EMBED_FILE})")
        return
    html = open(TEMPLATE_FILE, encoding="utf-8").read()
    notes = []

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo and URL_REGION_RE.search(html):
        url = f"https://cdn.jsdelivr.net/gh/{repo}@main/menu.json"
        html = URL_REGION_RE.sub(lambda m: f"{m.group(1)}'{url}'{m.group(2)}", html, count=1)
        notes.append(f"live link -> {url}")
    elif not repo:
        notes.append("live link left as-is (not running in GitHub Actions)")

    if FALLBACK_REGION_RE.search(html):
        payload = json.dumps(menu_payload, indent=2, sort_keys=True).replace("</", "<\\/")
        html = FALLBACK_REGION_RE.sub(lambda m: f"{m.group(1)}{payload}{m.group(2)}", html, count=1)
        n = sum(len(d) for d in (menu_payload.get("schools") or {}).values())
        notes.append(f"saved copy -> {n} day(s)")
    else:
        notes.append(f"WARNING: no saved-copy markers found in {TEMPLATE_FILE}")

    before = open(EMBED_FILE, encoding="utf-8").read() if os.path.exists(EMBED_FILE) else None
    if html != before:
        open(EMBED_FILE, "w", encoding="utf-8").write(html)
        print(f"Wrote {EMBED_FILE}: " + "; ".join(notes))
    else:
        print(f"{EMBED_FILE} unchanged: " + "; ".join(notes))


# --- Main ------------------------------------------------------------------

def main():
    today = date.today()
    # Start at whichever is earlier: a week back, or the 1st of this month,
    # so the month view always has the full current month.
    start = min(today - timedelta(days=DAYS_BEHIND), today.replace(day=1))
    end = today + timedelta(days=DAYS_AHEAD)
    fmt = "%m-%d-%Y"
    print(f"Window: {start.isoformat()} .. {end.isoformat()}")

    schools, failures, raw_for_dump = {}, [], None

    for key in SCHOOL_KEYS:
        url = API.format(key=key, start=start.strftime(fmt), end=end.strftime(fmt))
        print(f"\nFetching {key}\n  {url}")
        try:
            payload = fetch_json(url)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            failures.append(key)
            continue

        if raw_for_dump is None:
            raw_for_dump = payload

        name = school_name(payload) or key
        days = mark_featured(extract_days(payload))
        meals = sorted({m for d in days.values() for m in d})
        total = sum(len(items) for d in days.values() for items in d.values())
        print(f"  school: {name}")
        print(f"  {len(days)} day(s), meal periods: {meals or '(none)'}, {total} item(s)")

        if days:
            schools[name] = days
        else:
            print(f"  WARNING: no menu days parsed for {key}", file=sys.stderr)

    if not schools:
        print("\nERROR: no menu data parsed from any school.", file=sys.stderr)
        if raw_for_dump is not None:
            dump_structure(raw_for_dump)
        if os.path.exists("menu.json"):
            print("Keeping the existing menu.json rather than emptying it.", file=sys.stderr)
            # Still rebuild embed.html from the last good menu, so a fetch
            # failure never leaves Google Sites without a working page.
            try:
                build_embed(json.load(open("menu.json")))
            except Exception as e:
                print(f"Could not rebuild the embed pages from menu.json: {e}",
                      file=sys.stderr)
            sys.exit(1)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "MealViewer (api.mealviewer.com)",
        "schools": schools,
    }
    with open("menu.json", "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

    all_days = sorted({d for s in schools.values() for d in s})
    print(f"\nWrote menu.json: {len(schools)} school(s), {len(all_days)} day(s)")
    if all_days:
        print(f"Date range in data: {all_days[0]} .. {all_days[-1]}")
    if failures:
        print(f"WARNING: {len(failures)} school key(s) failed: {failures}", file=sys.stderr)

    build_embed(payload)


if __name__ == "__main__":
    main()
