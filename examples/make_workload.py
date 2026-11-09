"""Generate the example workload: five kinds of real production LLM traffic.

Every case except `rewrite` has a machine-checkable answer. The output is seeded,
so the file is reproducible.

    python examples/make_workload.py > examples/workload.jsonl
"""

import datetime
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
        "Route this support ticket. Reply with exactly one word.\n"
        "billing: charges, invoices, refunds and payments for an existing customer\n"
        "technical: bugs, errors, crashes and outages in the product\n"
        "account: logins, passwords, users, ownership and account changes\n"
        "sales: pricing, quotes, demos and reviews before buying\n\n"
        f"Ticket: {ticket}"
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
    a, b, c = rng.randint(30, 90), rng.randint(3, 24), rng.randint(2, 9)
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
    ("clamp_all(values)", "returns the numbers limited to the range [{a}, {b}]",
     "assert clamp_all([{a}-1, {a}, {b}+1]) == [{a}, {a}, {b}]\nassert clamp_all([]) == []"),
    ("chunk(items, size)", "splits a list into consecutive sublists of that size, where the last may be shorter",
     "assert chunk(list(range({c}+1)), {c}) == [list(range({c})), [{c}]]\nassert chunk([], {c}) == []"),
    ("is_balanced(text)", "returns True if the string's (), [] and {{}} brackets are balanced",
     "assert is_balanced('([{{}}])')\nassert not is_balanced('(]')\nassert not is_balanced('((')"),
    ("rle(text)", "run-length encodes a string, so 'aab' becomes 'a2b1'",
     "assert rle('aab') == 'a2b1'\nassert rle('') == ''\nassert rle('z'*{c}) == 'z{c}'"),
    ("top_k(items, k)", "returns the k most common items, most common first, with ties broken by first appearance",
     "assert top_k(list('aabbbc'), 2) == ['b', 'a']\nassert top_k(list('abc'), 1) == ['a']"),
    ("flatten(items)", "flattens arbitrarily nested lists into one list",
     "assert flatten([1,[2,[3,[{c}]]]]) == [1,2,3,{c}]\nassert flatten([]) == []"),
    ("roman(number)", "converts an integer from 1 to 3999 to a Roman numeral",
     "assert roman(1994) == 'MCMXCIV'\nassert roman(4) == 'IV'\nassert roman(3888) == 'MMMDCCCLXXXVIII'"),
    ("merge_ranges(ranges)", "merges overlapping (start, end) tuples and returns them as a sorted list of tuples",
     "assert merge_ranges([(1,3),(2,{c}+3),(20,21)]) == [(1,{c}+3),(20,21)]\nassert merge_ranges([]) == []"),
    ("is_palindrome(text)", "returns True if the string reads the same both ways, ignoring case and anything not alphanumeric",
     "assert is_palindrome('A man, a plan, a canal: Panama')\nassert not is_palindrome('ab')\nassert is_palindrome('')"),
    ("word_count(text)", "returns a dict counting each lowercased whitespace-separated word",
     "assert word_count('a A b') == {{'a': 2, 'b': 1}}\nassert word_count('') == {{}}"),
    ("fizzbuzz(n)", "returns the FizzBuzz strings for 1 to n as a list",
     "assert fizzbuzz(5) == ['1','2','Fizz','4','Buzz']\nassert fizzbuzz(15)[-1] == 'FizzBuzz'"),
    ("gcd_list(numbers)", "returns the greatest common divisor of a non-empty list of positive integers",
     "assert gcd_list([12, 18, {c}*6]) == 6\nassert gcd_list([7]) == 7"),
    ("primes_up_to(n)", "returns all primes less than or equal to n, in order",
     "assert primes_up_to(20) == [2,3,5,7,11,13,17,19]\nassert primes_up_to(1) == []"),
    ("rotate(items, k)", "rotates a list right by k places and returns a new list, for any k >= 0",
     "assert rotate([1,2,3,4], 1) == [4,1,2,3]\nassert rotate([1,2,3], 3) == [1,2,3]\nassert rotate([], {c}) == []"),
    ("transpose(matrix)", "transposes a rectangular list of lists",
     "assert transpose([[1,2,3],[4,5,6]]) == [[1,4],[2,5],[3,6]]\nassert transpose([]) == []"),
    ("binary_search(items, target)", "returns the index of target in a sorted list, or -1 if it is absent",
     "assert binary_search([1,3,5,7], 5) == 2\nassert binary_search([1,3], 2) == -1\nassert binary_search([], 1) == -1"),
    ("to_snake(name)", "converts a camelCase name to snake_case",
     "assert to_snake('fooBarBaz') == 'foo_bar_baz'\nassert to_snake('foo') == 'foo'"),
    ("dedupe(items)", "removes duplicates from a list, keeping the first occurrence of each",
     "assert dedupe([3,1,3,2,1]) == [3,1,2]\nassert dedupe([]) == []"),
    ("caesar(text, shift)", "shifts letters forward by shift, wrapping around, keeping case and leaving everything else alone",
     "assert caesar('xyz', 3) == 'abc'\nassert caesar('A b!', 1) == 'B c!'\nassert caesar(caesar('Hello, World', {c}), 26-{c}) == 'Hello, World'"),
    ("parse_kv(text)", "parses a string like 'a=1;b=2' into a dict of strings, where an empty string gives an empty dict",
     "assert parse_kv('a=1;b=2') == {{'a': '1', 'b': '2'}}\nassert parse_kv('') == {{}}"),
    ("moving_average(values, window)", "returns the mean of every full window, in order",
     "assert moving_average([1,2,3,4], 2) == [1.5,2.5,3.5]\nassert moving_average([1], 2) == []"),
    ("longest_common_prefix(words)", "returns the longest prefix shared by every word, or '' for an empty list",
     "assert longest_common_prefix(['flower','flow','flight']) == 'fl'\nassert longest_common_prefix(['a','b']) == ''\nassert longest_common_prefix([]) == ''"),
    ("group_by_length(words)", "returns a dict from word length to the words of that length, in their original order",
     "assert group_by_length(['a','bb','c']) == {{1: ['a','c'], 2: ['bb']}}\nassert group_by_length([]) == {{}}"),
    ("valid_ipv4(text)", "returns True for a valid dotted IPv4 address, where parts with leading zeros are invalid",
     "assert valid_ipv4('192.168.0.1')\nassert not valid_ipv4('256.1.1.1')\nassert not valid_ipv4('1.2.3')\nassert not valid_ipv4('01.2.3.4')"),
]
FRAMINGS = [
    "Write a Python function {signature} that {description}.",
    "Implement {signature} in Python. It {description}.",
    "For our utils module, I need {signature}, which {description}.",
    "Please write the Python function {signature}: it {description}.",
]


def code(index):
    signature, description, tests = CODE[index % len(CODE)]
    framing = FRAMINGS[(index // len(CODE)) % len(FRAMINGS)]
    a, b, c = rng.randint(-5, 2), rng.randint(5, 20), rng.randint(2, 6)
    values = {"a": a, "b": b, "c": c}
    prompt = framing.format(signature=signature, description=description.format(**values))
    prompt += " Reply with one python code block and nothing else."
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


def hard_math(index):
    """Multi-step problems that are easy to slip on without working carefully."""
    kind = index % 4
    if kind == 0:
        limit = rng.randint(2000, 9000)
        a, b, c = rng.choice([(3, 7, 5), (4, 9, 7), (6, 11, 5), (5, 13, 3), (7, 8, 9)])
        answer = sum(1 for n in range(1, limit + 1) if (n % a == 0 or n % b == 0) and n % c)
        text = (f"How many integers from 1 to {limit} inclusive are divisible by {a} or {b} "
                f"but not by {c}?")
        tolerance = 0.0
    elif kind == 1:
        value = rng.randint(100, 900)
        steps = [f"Start with {value}."]
        for _ in range(7):
            op, x = rng.choice(["add", "mul", "sub", "div", "mod"]), rng.randint(3, 17)
            if op == "add":
                value += x
                steps.append(f"Add {x}.")
            elif op == "mul":
                value *= x
                steps.append(f"Multiply by {x}.")
            elif op == "sub":
                value -= x
                steps.append(f"Subtract {x}.")
            elif op == "div":
                value //= x
                steps.append(f"Divide by {x}, rounding down.")
            else:
                x += 90
                value = value % x + x
                steps.append(f"Replace the value with its remainder when divided by {x}, "
                             f"then add {x}.")
        answer, tolerance = value, 0.0
        text = " ".join(steps) + " What is the result?"
    elif kind == 2:
        start = datetime.date(2024, 1, 1) + datetime.timedelta(days=rng.randint(0, 700))
        end = start + datetime.timedelta(days=rng.randint(200, 1500))
        days = [start + datetime.timedelta(days=n) for n in range((end - start).days + 1)]
        answer = sum(1 for day in days if day.weekday() == 0 and day.day <= 7)
        text = (f"Counting both end dates, how many first Mondays of a month fall between "
                f"{start.isoformat()} and {end.isoformat()}?")
        tolerance = 0.0
    else:
        principal, rate = rng.randint(2000, 20000), rng.choice([3, 4, 5, 6, 9])
        first, second = rng.randint(3, 9), rng.randint(3, 9)
        withdrawal = rng.randint(200, 1500)
        balance = principal * (1 + rate / 1200) ** first - withdrawal
        answer = round(balance * (1 + rate / 1200) ** second, 2)
        text = (f"${principal} earns {rate}% annual interest compounded monthly for {first} "
                f"months. Then ${withdrawal} is withdrawn, and the rest earns the same rate "
                f"for {second} more months. What is the final balance, to the cent?")
        tolerance = 0.02
    prompt = f"{text} Work it out, then end with the final number on its own line."
    return "hard-math", prompt, {"type": "number", "expected": answer, "tolerance": tolerance}


CUSTOMERS = ["Acme", "Birch", "Cobalt", "Delta", "Ember"]


def hard_extract(index):
    """A messy order log, where every total depends on reading each line correctly."""
    while True:
        lines, net, cancelled, shipped = [], {}, 0.0, 0
        for number in range(rng.randint(8, 12)):
            customer = rng.choice(CUSTOMERS)
            quantity, price = rng.randint(1, 9), rng.randint(5, 80) + rng.choice([0, 0.5, 0.25])
            status = rng.choice(["shipped", "shipped", "shipped", "cancelled", "returned"])
            discount = rng.choice([0, 0, 10, 20])
            value = quantity * price * (100 - discount) / 100
            note = f" (discount {discount}%)" if discount else ""
            lines.append(f"#{1000 + number} {customer}: {quantity} x {price:.2f}{note} - {status}")
            if status == "shipped":
                shipped += 1
                net[customer] = net.get(customer, 0.0) + value
            elif status == "returned":
                net[customer] = net.get(customer, 0.0) - value
            else:
                cancelled += value
        ranked = sorted(net.values(), reverse=True)
        if len(ranked) > 1 and ranked[0] - ranked[1] > 1:
            break
    expected = {
        "shipped_orders": shipped,
        "net_revenue": round(sum(net.values()), 2),
        "cancelled_value": round(cancelled, 2),
        "top_customer": max(net, key=net.get),
    }
    prompt = (
        "From this order log, return JSON with the keys shipped_orders (a count), "
        "net_revenue (shipped value minus returned value), cancelled_value and "
        "top_customer (the highest net of shipped minus returned). Values are quantity "
        "times price after any discount. Reply with JSON only.\n\n" + "\n".join(lines)
    )
    return "hard-extract", prompt, {"type": "json_fields", "strict": True, "tolerance": 0.02,
                                    "expected": expected}


PLAN = [(classify, 200), (extract, 200), (math_problem, 200), (code, 96), (rewrite, 100),
        (hard_math, 120), (hard_extract, 120)]

for make, count in PLAN:
    seen = set()
    index = attempts = 0
    while len(seen) < count and attempts < count * 50:
        attempts += 1
        segment, prompt, grader = make(index)
        if prompt in seen:
            continue
        seen.add(prompt)
        print(json.dumps({
            "id": f"{segment}-{index:03d}",
            "segment": segment,
            "messages": [{"role": "user", "content": prompt}],
            "grader": grader,
            # Reasoning models need room here, or they are cut off before answering.
            **({"params": {"max_tokens": 16000}} if segment.startswith("hard") else {}),
        }))
        index += 1
