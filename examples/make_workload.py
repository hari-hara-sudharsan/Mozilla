"""Generate the example workload: five kinds of real production LLM traffic.

Every case except `rewrite` has a machine-checkable answer. The output is seeded,
so the file is reproducible.

    python examples/make_workload.py > examples/workload.jsonl
"""

import json
import random

rng = random.Random(20261106)

TICKETS = {
    "billing": [
        "I was charged twice for my {plan} plan this month.",
        "Why did my invoice go up by ${n} after the renewal?",
        "Please refund the {plan} upgrade, I cancelled it the same day.",
        "My card ending {n}{n} was declined but the charge still shows as pending.",
    ],
    "technical": [
        "The {feature} page returns a 500 error every time I open it.",
        "Sync has been stuck at {n}% since this morning.",
        "After the update, {feature} crashes on startup.",
        "Our webhook stopped firing for {feature} events around {n}:00.",
    ],
    "account": [
        "I need to change the owner of our {plan} workspace.",
        "I can't reset my password, the email never arrives.",
        "How do I remove a former employee from {feature}?",
        "Please merge my two accounts, both are on the {plan} plan.",
    ],
    "sales": [
        "We have {n}0 seats to buy. Is there volume pricing for {plan}?",
        "Can we get a quote and a security review for {feature}?",
        "Does the {plan} plan include SSO? We're comparing vendors.",
        "We'd like a demo of {feature} for our team next week.",
    ],
}
PLANS = ["Starter", "Team", "Business", "Enterprise"]
FEATURES = ["reports", "dashboard", "exports", "calendar", "integrations"]


def classify(index):
    label = rng.choice(sorted(TICKETS))
    ticket = rng.choice(TICKETS[label]).format(
        plan=rng.choice(PLANS), feature=rng.choice(FEATURES), n=rng.randint(2, 9)
    )
    prompt = (
        "Route this support ticket. Reply with exactly one word: "
        f"billing, technical, account or sales.\n\nTicket: {ticket}"
    )
    return "classify", prompt, {"type": "exact", "expected": label}


VENDORS = ["Northwind Traders", "Halden & Sons", "Bluefin Logistics", "Orchard Print Co", "Kestrel Labs"]
CURRENCIES = ["USD", "EUR", "GBP"]


def extract(index):
    vendor = rng.choice(VENDORS)
    number = f"INV-{rng.randint(10000, 99999)}"
    currency = rng.choice(CURRENCIES)
    quantity, unit = rng.randint(2, 40), rng.randint(5, 250)
    total = quantity * unit
    date = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    text = (
        f"From {vendor}. Invoice {number}, issued {date}. "
        f"{quantity} units at {unit} {currency} each. Amount due: {total} {currency}. "
        f"Payment terms are net {rng.choice([15, 30, 45])} days."
    )
    prompt = (
        "Extract this invoice as JSON with the keys vendor, invoice_number, date, currency "
        f"and total (a number). Reply with JSON only.\n\n{text}"
    )
    expected = {"vendor": vendor, "invoice_number": number, "date": date,
                "currency": currency, "total": total}
    return "extract", prompt, {"type": "json_fields", "strict": True, "expected": expected}


def math_problem(index):
    kind = index % 3
    a, b, c = rng.randint(12, 90), rng.randint(3, 24), rng.randint(2, 9)
    if kind == 0:
        text = (f"A warehouse holds {a * c} boxes. It ships {b} boxes a day for {c} days, "
                f"then receives {a} more. How many boxes does it hold now?")
        answer = a * c - b * c + a
    elif kind == 1:
        text = (f"A subscription costs ${a} a month with a {b}% annual discount if paid yearly. "
                "How many dollars does a full year cost when paid yearly? Round to 2 decimals.")
        answer = round(a * 12 * (100 - b) / 100, 2)
    else:
        text = (f"A train travels {a} km at {b + 40} km/h, then {a + b} km at {b + 60} km/h. "
                "How many hours does the trip take in total? Round to 2 decimals.")
        answer = round(a / (b + 40) + (a + b) / (b + 60), 2)
    prompt = f"{text} Work it out, then end with the final number on its own line."
    return "math", prompt, {"type": "number", "expected": answer, "tolerance": 0.011}


CODE = [
    ("clamp_all", "takes a list of numbers and returns them limited to the range [{a}, {b}]",
     "assert clamp_all([{a}-1, {a}, {b}+1]) == [{a}, {a}, {b}]\nassert clamp_all([]) == []"),
    ("chunk", "splits a list into consecutive sublists of length {c}, the last may be shorter",
     "assert chunk(list(range({c}+1)), {c}) == [list(range({c})), [{c}]]\nassert chunk([], {c}) == []"),
    ("is_balanced", "returns True if a string's (), [] and {{}} brackets are balanced",
     "assert is_balanced('([{{}}])')\nassert not is_balanced('(]')\nassert not is_balanced('((')"),
    ("rle", "run-length encodes a string, so 'aab' becomes 'a2b1'",
     "assert rle('aab') == 'a2b1'\nassert rle('') == ''\nassert rle('z'*{c}) == 'z{c}'"),
    ("top_k", "returns the {c} most common items in a list, ties broken by first appearance",
     "assert top_k(list('aabbbc')+list('defghij'), {c})[0] == 'b'"),
    ("flatten", "flattens arbitrarily nested lists into one list",
     "assert flatten([1,[2,[3,[{c}]]]]) == [1,2,3,{c}]\nassert flatten([]) == []"),
    ("roman", "converts an integer from 1 to 3999 to a Roman numeral",
     "assert roman(1994) == 'MCMXCIV'\nassert roman(4) == 'IV'\nassert roman(3888) == 'MMMDCCCLXXXVIII'"),
    ("merge_ranges", "merges overlapping (start, end) tuples and returns them sorted",
     "assert merge_ranges([(1,3),(2,{c}+3),(20,21)]) == [(1,{c}+3),(20,21)]"),
]


def code(index):
    name, description, tests = CODE[index % len(CODE)]
    a, b, c = rng.randint(-5, 2), rng.randint(5, 20), rng.randint(2, 6)
    values = {"a": a, "b": b, "c": c}
    prompt = (
        f"Write a Python function named {name} that {description.format(**values)}. "
        "Reply with one python code block and nothing else."
    )
    return "code", prompt, {"type": "python_tests", "tests": tests.format(**values)}


NOTES = [
    "tell the customer their refund is delayed {n} days because of the bank",
    "ask the team to finish their timesheets by friday or payroll slips",
    "let a client know the launch moved {n} weeks and why that's a good thing",
    "decline a meeting politely and offer to reply by email",
    "thank a colleague for covering the on-call shift over the holiday",
    "warn users about {n} hours of maintenance on sunday night",
]
TONES = ["warm", "formal", "concise", "reassuring"]


def rewrite(index):
    note = rng.choice(NOTES).format(n=rng.randint(2, 9))
    prompt = f"Write a short {rng.choice(TONES)} email to {note}. Three sentences at most."
    reference = (
        "Hello, thank you for your patience. Here is a clear and considerate update on the "
        "situation, along with what happens next and who to contact with any questions."
    )
    return "rewrite", prompt, {"type": "jury", "reference": reference}


PLAN = [(classify, 90), (extract, 90), (math_problem, 90), (code, 64), (rewrite, 60)]

for make, count in PLAN:
    for index in range(count):
        segment, prompt, grader = make(index)
        print(json.dumps({
            "id": f"{segment}-{index:03d}",
            "segment": segment,
            "messages": [{"role": "user", "content": prompt}],
            "grader": grader,
        }))
