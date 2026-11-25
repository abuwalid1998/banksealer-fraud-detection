"""Mock online-banking transaction generator.

Simulates a population of customers of one or more banks with stable, personal
habits (beneficiaries, amounts, devices/ASNs, active hours) for three operation
types studied by BankSealer: bank transfers, phone recharges and prepaid-card
top-ups. Fraud is injected with realistic scenarios:

* ``information_stealing``   - credentials stolen by a trojan/phishing; the fraudster
  connects from a foreign ASN at any hour and wires a large amount to a new foreign IBAN.
* ``transaction_hijacking``  - man-in-the-browser: the fraud runs inside the victim's own
  session (same IP/ASN, normal hours) but pays a new IBAN a larger-than-usual amount.
* ``salami_burst``           - several medium transfers to new IBANs within ~2 hours.
* ``recharge_fraud``         - burst of phone recharges to numbers never used before.
* ``prepaid_drain``          - large top-ups of a prepaid card never used before.

Records are emitted as plain dictionaries (the "raw" format of a channel), with
ground-truth columns ``is_fraud`` and ``scenario`` that the engine ignores.

The simulation runs on its own clock, day after day: ``history(days)`` produces
the past, ``live()`` continues the same simulation forward indefinitely.
"""
from __future__ import annotations

import math
import random
import zlib
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator, Optional

HOME_ASNS = {
    "IT": [("AS3269", (79, 151)), ("AS30722", (5, 87)), ("AS1267", (151, 2))],
    "DE": [("AS3320", (84, 91)), ("AS3209", (88, 64)), ("AS6805", (89, 12))],
    "FR": [("AS3215", (90, 2)), ("AS12322", (82, 64)), ("AS15557", (78, 192))],
    "PS": [("AS12975", (213, 6)), ("AS15975", (213, 244)), ("AS51407", (37, 8))],
}
FOREIGN_ASNS = [("NL", "AS60781", (37, 48)), ("US", "AS14061", (159, 65)), ("RU", "AS12389", (95, 24)),
                ("RO", "AS8708", (86, 120)), ("NG", "AS37148", (197, 210)), ("PA", "AS52468", (190, 97)),
                ("UA", "AS15895", (46, 211)), ("BR", "AS28573", (177, 32))]
FRAUD_IBAN_COUNTRIES = ["LT", "RO", "CY", "MT", "BG", "GB", "KY", "PA", "RU", "AE"]
EU_IBAN_COUNTRIES = ["DE", "FR", "ES", "NL", "AT", "BE", "IT", "PT"]
BBAN_LENGTH = {"IT": 23, "DE": 18, "FR": 23, "ES": 20, "NL": 14, "AT": 16, "BE": 12, "PT": 21, "LT": 16,
               "RO": 20, "CY": 24, "MT": 27, "BG": 18, "GB": 18, "KY": 26, "PA": 20, "RU": 29, "AE": 19,
               "PS": 25, "JO": 26}
OPERATORS = {"IT": ["TIM", "Vodafone", "WindTre", "Iliad"], "DE": ["Telekom", "Vodafone", "O2"],
             "FR": ["Orange", "SFR", "Bouygues", "Free"], "PS": ["Jawwal", "Ooredoo"]}
PHONE_PREFIX = {"IT": "+39 3", "DE": "+49 15", "FR": "+33 6", "PS": "+970 59"}
RECHARGE_AMOUNTS = [5, 10, 15, 20, 25, 30, 50]
DEFAULT_BANKS = {"BANK001": "IT", "BANK002": "DE"}

# --------------------------------------------------------------------------- Palestine market profile
# Realistic retail/business banking behaviour for a Palestinian bank (amounts in ILS): Sat-Thu working
# week (Friday is the quiet day), salary-week peaks, local bank codes in IBANs, Jawwal / Ooredoo
# recharges, named beneficiaries with payment purposes, city, channel and device per customer.
PS_BANK_CODES = ["PALS", "PALS", "PALS", "ARAB", "QUDS", "PIBC", "CAAB", "BJOR", "SAFA", "ARIB", "TNBP"]
PS_CITIES = ["Ramallah", "Ramallah", "Al-Bireh", "Nablus", "Nablus", "Hebron", "Hebron", "Bethlehem", "Jenin",
             "Tulkarm", "Qalqilya", "Jericho", "Gaza", "Khan Younis", "Salfit", "Tubas"]
PS_FIRST = ["Mohammad", "Ahmad", "Mahmoud", "Omar", "Khaled", "Yousef", "Ibrahim", "Rami", "Sami", "Tariq",
            "Fadi", "Hani", "Nidal", "Basel", "Majd", "Ali", "Hasan", "Amjad", "Saleh", "Kareem",
            "Fatima", "Maryam", "Aya", "Rana", "Hala", "Lina", "Dana", "Reem", "Nour", "Salma", "Rawan",
            "Samah", "Ruba", "Ghada", "Leen", "Dima", "Haneen", "Asmaa", "Sireen", "Shireen"]
PS_LAST = ["Abu Ghosh", "Barghouti", "Khalil", "Hamdan", "Odeh", "Nasser", "Saleh", "Haddad", "Khoury",
           "Masri", "Shakaa", "Tamimi", "Jaber", "Darwish", "Awad", "Qasem", "Yasin", "Rabah", "Halabi",
           "Shami", "Mansour", "Zaid", "Abu Eid", "Natsheh", "Qawasmi", "Jabari", "Sharabati", "Totah",
           "Dajani", "Husseini", "Nashashibi", "Ayyad", "Abu Shanab", "Freij", "Kanaan"]
PS_COMPANIES = ["Al-Quds Trading Co.", "Nablus Stone & Marble", "Hebron Glass Industries", "Ramallah Foods Ltd.",
                "Bethlehem Olive Wood Crafts", "Jenin Agricultural Supplies", "Palestine Pharma Distribution",
                "Al-Bireh Construction Co.", "Jericho Date Farms", "Tulkarm Steel Works", "Gaza Furniture House",
                "Al-Amal Electronics", "Al-Nour Pharmacy", "Shams Solar Energy", "Al-Wataniya Logistics",
                "Al-Zaytouna Supermarket", "Carmel Textile Factory", "Al-Rawda Medical Center",
                "Bab Al-Sharq Restaurants", "Al-Manara Printing Press", "Kanaan Tech Solutions",
                "Al-Huda Auto Parts", "Al-Jazeera Building Materials", "Sanabel Bakery"]
PS_PURPOSES_RETAIL = ["Family support", "Rent payment", "University tuition", "Loan instalment",
                      "Personal transfer", "Car instalment", "Medical expenses", "Wedding gift",
                      "School fees", "Savings transfer", "Household expenses", "Furniture purchase"]
PS_PURPOSES_BUSINESS = ["Supplier invoice", "Salaries", "Raw materials", "Goods purchase", "Customs clearance",
                        "Equipment purchase", "Transport services", "Office rent", "Maintenance contract",
                        "Wholesale order", "Advance payment", "Service fees"]
PS_FOREIGN_LEGIT = ["JO", "JO", "JO", "AE", "SA", "TR", "QA", "DE"]
PS_PHONE_PREFIX = {"Jawwal": ["+970 59"], "Ooredoo": ["+970 56"]}
PS_RECHARGES = [10, 20, 20, 25, 30, 50, 50, 100]
PS_CHANNELS = {"retail": ["mobile_banking"] * 6 + ["internet_banking"] * 2 + ["atm_kiosk", "branch"],
               "affluent": ["mobile_banking"] * 5 + ["internet_banking"] * 4 + ["branch"],
               "business": ["internet_banking"] * 6 + ["mobile_banking"] * 2 + ["branch"] * 2}
PS_DEVICES = ["iPhone 15", "iPhone 13", "Samsung Galaxy S23", "Samsung Galaxy A54", "Xiaomi Redmi Note 12",
              "Huawei P30", "Windows PC - Chrome", "Windows PC - Edge", "MacBook - Safari"]


PS_MERCHANTS = [  # (name, MCC, typical amount ILS) - fictional local merchants
    ("Al-Zaytouna Supermarket", "5411", 120), ("Bravo Market", "5411", 180), ("Shams Bakery", "5462", 25),
    ("Al-Quds Pharmacy", "5912", 70), ("Petrol Station Al-Masyoun", "5541", 200), ("Paltel Shop", "4814", 90),
    ("Ramallah Mall Fashion", "5651", 260), ("Abu Shukri Restaurant", "5812", 85), ("Cafe Jasmine", "5814", 30),
    ("Al-Amal Electronics", "5732", 650), ("Home Center Nablus", "5200", 400), ("Bethlehem Bookshop", "5942", 60),
    ("City Clinic", "8011", 150), ("Hebron Shoes", "5661", 220), ("Taxi Al-Nour", "4121", 35)]
ECOM_MERCHANTS = [("Global Online Marketplace", "5399", "CN", 140), ("SkyFly Tickets", "4511", "JO", 1200),
                  ("StreamBox Subscription", "4899", "US", 40), ("Istanbul Fashion Online", "5651", "TR", 300),
                  ("Gulf Electronics Online", "5732", "AE", 900)]
FRAUD_MERCHANTS = [("QuickGift Cards Ltd", "5999", "GB"), ("Crypto Exchange XT", "6051", "SC"),
                   ("Lucky Bet Online", "7995", "MT"), ("Digital Goods 24", "5818", "CY")]
ATM_PREFIX = {"Ramallah": "RAM", "Al-Bireh": "BIR", "Nablus": "NAB", "Hebron": "HEB", "Bethlehem": "BET",
              "Jenin": "JEN", "Tulkarm": "TUL", "Qalqilya": "QAL", "Jericho": "JER", "Gaza": "GAZ",
              "Khan Younis": "KHY", "Salfit": "SAL", "Tubas": "TUB"}


def ps_iban(rng: random.Random, bank: Optional[str] = None) -> str:
    """Palestinian IBAN: PS + check digits + 4-letter bank code + 21 digits (29 characters)."""
    bban = (bank or rng.choice(PS_BANK_CODES)) + "".join(rng.choices(string.digits, k=21))
    return f"PS{iban_check_digits('PS', bban)}{bban}"


def iban_check_digits(country: str, bban: str) -> str:
    rearranged = bban + country + "00"
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return f"{98 - int(digits) % 97:02d}"


def make_iban(rng: random.Random, country: str) -> str:
    length = BBAN_LENGTH.get(country, 18)
    if country == "IT":
        bban = rng.choice(string.ascii_uppercase) + "".join(rng.choices(string.digits, k=10)) + \
            "".join(rng.choices(string.digits + string.ascii_uppercase, k=12))
    elif country == "PS":
        return ps_iban(rng)
    elif country == "JO":
        bban = "".join(rng.choices(string.ascii_uppercase, k=4)) + "".join(rng.choices(string.digits, k=length - 4))
    else:
        bban = "".join(rng.choices(string.digits, k=length))
    return f"{country}{iban_check_digits(country, bban)}{bban}"


def make_ip(rng: random.Random, prefix: tuple[int, int]) -> str:
    return f"{prefix[0]}.{prefix[1]}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


@dataclass
class Customer:
    user_id: str
    bank_id: str
    home: str
    segment: str
    asns: list                      # [(asn, prefix)]
    ips: list                       # [(ip, cc_asn)]
    payees: list
    payee_weights: list
    transfer_rate: float
    amount_mu: float
    amount_sigma: float
    recharge_rate: float
    phones: list
    operator: str
    prepaid_rate: float
    cards: list
    prepaid_mu: float
    hour_mean: float
    hour_std: float
    weekend_factor: float = 0.6
    known: dict = field(default_factory=dict)
    # optional realism attributes (Palestine profile)
    name: str = ""
    city: str = ""
    payee_names: list = field(default_factory=list)
    channels: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    weekend_day: int = 5                      # first weekend weekday (5 = Saturday, 4 = Friday)
    card_rate: float = 0.0                    # card payments per day
    favourite_merchants: list = field(default_factory=list)
    ecom_share: float = 0.0
    cash_out_rate: float = 0.0
    cash_in_rate: float = 0.0
    cash_in_mu: float = 0.0
    salary: float = 0.0                       # monthly incoming salary (0 = none)
    employer_iban: str = ""
    remitter: tuple = ()                      # (iban, country, monthly amount)
    clients: list = field(default_factory=list)   # business: paying customers' IBANs
    receipts_rate: float = 0.0
    cheque_out_rate: float = 0.0              # own cheques presented per day
    cheque_in_rate: float = 0.0               # cheques deposited per day
    cheque_mu: float = 0.0
    cheque_seq: int = 0                       # next number of the customer's cheque book
    card_ref: str = ""                        # payment card token (masked)

    @property
    def daily_rate(self) -> float:
        return (self.transfer_rate + self.recharge_rate + self.prepaid_rate + self.card_rate + self.cash_out_rate
                + self.cash_in_rate + self.receipts_rate)


class TransactionGenerator:
    def __init__(self, n_customers: int = 150, banks: Optional[dict] = None, seed: Optional[int] = None,
                 fraud_ratio: float = 0.03, start: Optional[datetime] = None, currency: str = "EUR"):
        self.rng = random.Random(seed)
        self.seed = seed
        self.currency = currency
        self.banks = banks or dict(DEFAULT_BANKS)
        self.fraud_ratio = fraud_ratio
        self.customers = [self._make_customer(i) for i in range(n_customers)]
        # a small pool of mule accounts that fraudsters reuse – victims of different customers pay the same
        # accounts, which is what network analysis finds (separate RNG keeps the customer stream unchanged)
        mr = random.Random(f"mules-{seed}")
        self.mule_accounts = [make_iban(mr, mr.choice(FRAUD_IBAN_COUNTRIES)) for _ in range(6)]
        self._seq = 0
        today = (start or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        self.cursor = today   # next simulated day

    # ----------------------------------------------------------- personas
    def _make_customer(self, i: int) -> Customer:
        rng = self.rng
        bank_id = rng.choice(list(self.banks))
        home = self.banks[bank_id]
        if home == "PS":
            return self._make_ps_customer(i, bank_id)
        segment = rng.choices(["retail", "affluent", "business"], weights=[70, 20, 10])[0]
        asns = rng.sample(HOME_ASNS[home], k=rng.choice([1, 1, 2]))
        ips = []
        for asn, prefix in asns:
            for _ in range(rng.randint(1, 2)):
                ips.append((make_ip(rng, prefix), f"{home}-{asn}"))
        n_payees = {"retail": rng.randint(2, 6), "affluent": rng.randint(4, 10), "business": rng.randint(12, 30)}[segment]
        payees = [make_iban(rng, home if rng.random() < 0.88 else rng.choice(EU_IBAN_COUNTRIES)) for _ in range(n_payees)]
        payee_weights = [1.0 / (k + 1) ** 0.8 for k in range(n_payees)]   # a few favourite beneficiaries
        transfer_rate, mu, sigma = {
            "retail": (rng.uniform(0.15, 0.5), rng.uniform(4.0, 5.5), rng.uniform(0.3, 0.6)),
            "affluent": (rng.uniform(0.3, 0.9), rng.uniform(5.5, 6.8), rng.uniform(0.3, 0.6)),
            "business": (rng.uniform(1.0, 3.5), rng.uniform(6.2, 7.4), rng.uniform(0.4, 0.8)),
        }[segment]
        recharge_rate = rng.uniform(0.04, 0.2) if rng.random() < 0.6 else 0.0
        prepaid_rate = rng.uniform(0.03, 0.15) if rng.random() < 0.4 else 0.0
        phones = [f"{PHONE_PREFIX[home]}{rng.randint(10, 99)} {rng.randint(1000000, 9999999)}"
                  for _ in range(rng.choice([1, 1, 2]))]
        cards = [f"5355 **** **** {rng.randint(1000, 9999)}" for _ in range(rng.choice([1, 1, 2]))]
        hour_mean = rng.uniform(9.0, 19.0) if segment != "business" else rng.uniform(9.5, 15.0)
        return Customer(
            user_id=f"C{i + 1:05d}", bank_id=bank_id, home=home, segment=segment, asns=asns, ips=ips,
            payees=payees, payee_weights=payee_weights, transfer_rate=transfer_rate, amount_mu=mu,
            amount_sigma=sigma, recharge_rate=recharge_rate, phones=phones,
            operator=rng.choice(OPERATORS[home]), prepaid_rate=prepaid_rate, cards=cards,
            prepaid_mu=rng.uniform(3.5, 5.0), hour_mean=hour_mean, hour_std=rng.uniform(1.2, 3.0),
            weekend_factor=0.2 if segment == "business" else 0.7,
        )

    def _make_ps_customer(self, i: int, bank_id: str) -> Customer:
        rng = self.rng
        segment = rng.choices(["retail", "affluent", "business"], weights=[68, 20, 12])[0]
        asns = rng.sample(HOME_ASNS["PS"], k=rng.choice([1, 1, 2]))
        ips = [(make_ip(rng, prefix), f"PS-{asn}") for asn, prefix in asns for _ in range(rng.randint(1, 2))]
        n_payees = {"retail": rng.randint(2, 6), "affluent": rng.randint(4, 10), "business": rng.randint(10, 26)}[segment]
        foreign = {"retail": 0.05, "affluent": 0.12, "business": 0.18}[segment]
        payees = [make_iban(rng, "PS" if rng.random() >= foreign else rng.choice(PS_FOREIGN_LEGIT))
                  for _ in range(n_payees)]
        if segment == "business":
            payee_names = [rng.choice(PS_COMPANIES) if rng.random() < 0.6 else
                           f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}" for _ in payees]
            name = rng.choice(PS_COMPANIES)
        else:
            payee_names = [f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}" if rng.random() < 0.8 else
                           rng.choice(PS_COMPANIES) for _ in payees]
            name = f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}"
        payee_weights = [1.0 / (k + 1) ** 0.8 for k in range(n_payees)]
        transfer_rate, mu, sigma = {
            "retail": (rng.uniform(0.12, 0.45), rng.uniform(5.3, 6.5), rng.uniform(0.35, 0.65)),     # ~200-670 ILS
            "affluent": (rng.uniform(0.3, 0.85), rng.uniform(6.8, 7.9), rng.uniform(0.35, 0.6)),     # ~900-2,700
            "business": (rng.uniform(1.0, 3.2), rng.uniform(7.4, 8.4), rng.uniform(0.4, 0.7)),       # ~1,600-4,450
        }[segment]
        operator = rng.choices(["Jawwal", "Ooredoo"], weights=[65, 35])[0]
        phones = [f"{rng.choice(PS_PHONE_PREFIX[operator])}{rng.randint(0, 9)} {rng.randint(100000, 999999)}"
                  for _ in range(rng.choice([1, 1, 2]))]
        cards = [f"4{rng.randint(100, 999)} **** **** {rng.randint(1000, 9999)}" for _ in range(rng.choice([1, 1, 2]))]
        hour_mean = rng.uniform(9.0, 20.0) if segment != "business" else rng.uniform(9.0, 14.5)
        chans = PS_CHANNELS[segment]
        return Customer(
            user_id=f"C{i + 1:05d}", bank_id=bank_id, home="PS", segment=segment, asns=asns, ips=ips,
            payees=payees, payee_weights=payee_weights, transfer_rate=transfer_rate, amount_mu=mu,
            amount_sigma=sigma, recharge_rate=rng.uniform(0.05, 0.25) if rng.random() < 0.75 else 0.0,
            phones=phones, operator=operator,
            prepaid_rate=rng.uniform(0.02, 0.10) if rng.random() < 0.3 else 0.0, cards=cards,
            prepaid_mu=rng.uniform(4.6, 6.0), hour_mean=hour_mean, hour_std=rng.uniform(1.3, 3.0),
            weekend_factor=0.15 if segment == "business" else 0.55, name=name, city=rng.choice(PS_CITIES),
            payee_names=payee_names, channels=[rng.choice(chans) for _ in range(3)],
            devices=[f"{m} #{zlib.crc32(f'{self.seed}-{i}-{m}'.encode()):08X}"
                     for m in rng.sample(PS_DEVICES, k=rng.choice([1, 1, 2]))], weekend_day=4,
            **self._ps_extra(i, segment))

    def _ps_extra(self, i: int, segment: str) -> dict:
        """Card, cash and incoming-money habits (separate RNG keeps the original persona attributes stable)."""
        r = random.Random(f"{self.seed}-extra-{i}")
        business = segment == "business"
        salaried = not business and r.random() < 0.7
        out = {"card_rate": {"retail": r.uniform(0.3, 1.1), "affluent": r.uniform(0.8, 2.0),
                             "business": r.uniform(0.1, 0.5)}[segment],
               "favourite_merchants": r.sample(range(len(PS_MERCHANTS)), k=r.randint(4, 8)),
               "ecom_share": r.uniform(0.0, 0.15) if segment != "business" else r.uniform(0.0, 0.3),
               "cash_out_rate": r.uniform(0.08, 0.35) if not business else r.uniform(0.05, 0.2),
               "cash_in_rate": r.uniform(0.15, 0.8) if business else (r.uniform(0.01, 0.05) if r.random() < 0.3 else 0),
               "cash_in_mu": r.uniform(7.2, 8.3) if business else r.uniform(6.0, 7.2),
               "salary": round(r.uniform(2800, 9000) if salaried and segment == "retail" else
                               r.uniform(9000, 22000) if salaried else 0, -1),
               "employer_iban": ps_iban(r) if salaried else ""}
        if not business and r.random() < 0.25:
            cc = r.choice(["JO", "AE", "SA", "QA", "US", "TR"])
            out["remitter"] = (make_iban(r, cc), cc, round(r.uniform(500, 3000), -1))
        if business:
            out["clients"] = [ps_iban(r) for _ in range(r.randint(8, 30))]
            out["receipts_rate"] = r.uniform(0.4, 2.0)
        # cheques (still common in Palestine) and the payment card token
        if business:
            out.update(cheque_out_rate=r.uniform(0.15, 0.6), cheque_in_rate=r.uniform(0.2, 0.8),
                       cheque_mu=r.uniform(7.6, 8.7))
        else:
            out.update(cheque_out_rate=r.uniform(0.01, 0.05) if r.random() < 0.35 else 0.0,
                       cheque_in_rate=r.uniform(0.01, 0.04) if r.random() < 0.3 else 0.0,
                       cheque_mu=r.uniform(6.8, 8.0))
        out["cheque_seq"] = 100000 + r.randint(0, 8000) * 100
        out["card_ref"] = f"CARD-{i:05d}-{r.randint(1000, 9999)}"
        return out

    # -------------------------------------------------------------- helpers
    def _next_id(self) -> str:
        self._seq += 1
        return f"MOCK-{self._seq:08d}-{uuid.UUID(int=self.rng.getrandbits(128)).hex[:6]}"

    def _time(self, day: datetime, c: Customer) -> datetime:
        hour = self.rng.gauss(c.hour_mean, c.hour_std)
        hour = min(max(hour, 6.0), 23.5)
        return day + timedelta(hours=hour, seconds=self.rng.randint(0, 59))

    def _base(self, c: Customer, ts: datetime, tx_type: str, amount: float, ip: tuple, fraud: str | None) -> dict:
        rec = {
            "tx_id": self._next_id(), "bank_id": c.bank_id, "user_id": c.user_id,
            "timestamp": ts.isoformat(sep=" ", timespec="seconds"), "tx_type": tx_type,
            "amount": round(amount, 2), "currency": self.currency, "iban": None, "ip": ip[0], "cc_asn": ip[1],
            "session_id": uuid.UUID(int=self.rng.getrandbits(128)).hex[:16], "card_id": None,
            "phone_number": None, "operator": None, "is_fraud": 1 if fraud else 0, "scenario": fraud or "",
        }
        if c.city:
            channel = self.rng.choice(c.channels)
            if channel == "branch" and not (8 <= ts.hour < 15 and ts.weekday() != 4):
                channel = "mobile_banking"          # branches are open Sat-Thu 08:00-15:00
            if fraud and ip not in c.ips:
                channel, device = self.rng.choice(["mobile_banking", "internet_banking"]), "Unknown device"
            else:
                device = {"branch": "Branch counter", "atm_kiosk": "ATM / kiosk"}.get(channel) or \
                    self.rng.choice(c.devices)
            rec.update({"channel": channel, "city": c.city, "device": device})
        return rec

    def _home_ip(self, c: Customer) -> tuple:
        return self.rng.choices(c.ips, weights=[3] + [1] * (len(c.ips) - 1))[0]

    def _foreign_ip(self) -> tuple:
        cc, asn, prefix = self.rng.choice(FOREIGN_ASNS)
        return make_ip(self.rng, prefix), f"{cc}-{asn}"

    def _median_amount(self, c: Customer) -> float:
        return math.exp(c.amount_mu)

    # ------------------------------------------------------------- legit
    def _legit_day(self, c: Customer, day: datetime) -> list[dict]:
        rng = self.rng
        if c.city:   # Palestine profile: Friday is the quiet day, Saturday reduced; salary week busier
            wd = day.weekday()
            factor = c.weekend_factor if wd == 4 else (0.8 if wd == 5 and c.segment != "business" else 1.0)
            if day.day <= 5 or day.day >= 27:
                factor *= 1.35
        else:
            factor = c.weekend_factor if day.weekday() >= 5 else 1.0
        cap = 14000.0 if c.city else 9500.0
        out = []
        for _ in range(self._poisson(c.transfer_rate * factor)):
            tx = self._base(c, self._time(day, c), "bank_transfer",
                            min(rng.lognormvariate(c.amount_mu, c.amount_sigma), cap), self._home_ip(c), None)
            k = rng.choices(range(len(c.payees)), weights=c.payee_weights)[0]
            tx["iban"] = c.payees[k]
            if c.city:
                tx["amount"] = self._round_amount(tx["amount"])
                tx["beneficiary_name"] = c.payee_names[k]
                tx["purpose"] = rng.choice(PS_PURPOSES_BUSINESS if c.segment == "business" else PS_PURPOSES_RETAIL)
            out.append(tx)
        for _ in range(self._poisson(c.recharge_rate * factor)):
            amount = rng.choice(PS_RECHARGES) if c.city else rng.choice(RECHARGE_AMOUNTS[:5])
            tx = self._base(c, self._time(day, c), "phone_recharge", amount,
                            self._home_ip(c), None)
            tx["phone_number"] = rng.choices(c.phones, weights=[4] + [1] * (len(c.phones) - 1))[0]
            tx["operator"] = c.operator
            out.append(tx)
        if c.city:
            out.extend(self._ps_extra_day(c, day, factor))
        for _ in range(self._poisson(c.prepaid_rate * factor)):
            tx = self._base(c, self._time(day, c), "prepaid_card", min(rng.lognormvariate(c.prepaid_mu, 0.4), 900.0),
                            self._home_ip(c), None)
            tx["card_id"] = rng.choice(c.cards)
            out.append(tx)
        return out

    def _ps_extra_day(self, c: Customer, day: datetime, factor: float) -> list[dict]:
        rng = self.rng
        out = []
        for _ in range(self._poisson(c.card_rate * factor)):
            if rng.random() < c.ecom_share:
                name, mcc, cc, typical = rng.choice(ECOM_MERCHANTS)
                mode = "ECOM"
            else:
                name, mcc, typical = PS_MERCHANTS[rng.choice(c.favourite_merchants)]
                cc, mode = "PS", rng.choices(["CHIP", "CONTACTLESS"], weights=[45, 55])[0]
            tx = self._base(c, self._time(day, c), "card_payment", max(3.0, rng.lognormvariate(math.log(typical), 0.5)),
                            self._home_ip(c), None)
            tx.update({"merchant": name, "mcc": mcc, "merchant_cc": cc, "entry_mode": mode,
                       "channel": "card_ecom" if mode == "ECOM" else "card_pos",
                       "device": rng.choice(c.devices) if mode == "ECOM" else "Card terminal",
                       "amount": round(tx["amount"], 2), "card_ref": c.card_ref})
            out.append(tx)
        for _ in range(self._poisson(c.cash_out_rate * factor)):
            city = c.city if rng.random() < 0.85 else rng.choice(PS_CITIES)
            amount = float(rng.choice([100, 200, 200, 300, 500, 500, 700, 1000, 1500, 2000]))
            tx = self._base(c, self._time(day, c), "cash_withdrawal", amount, self._home_ip(c), None)
            tx.update({"terminal": f"ATM-{ATM_PREFIX.get(city, 'PS')}-{(zlib.crc32((c.user_id + city).encode()) % 20) + 1:03d}",
                       "location": city, "channel": "atm", "device": "ATM / kiosk", "card_ref": c.card_ref})
            out.append(tx)
        for _ in range(self._poisson(c.cash_in_rate * factor)):
            tx = self._base(c, day + timedelta(hours=rng.uniform(8.5, 14.5)), "cash_deposit",
                            self._round_amount(min(rng.lognormvariate(c.cash_in_mu, 0.5), 45000.0)), self._home_ip(c),
                            None)
            tx.update({"terminal": f"BR-{ATM_PREFIX.get(c.city, 'PS')}-01", "location": c.city, "channel": "branch",
                       "device": "Branch counter"})
            out.append(tx)
        if c.salary and day.day == 1 + (int(c.user_id[1:]) % 4):
            tx = self._base(c, day + timedelta(hours=rng.uniform(7, 10)), "incoming_transfer",
                            c.salary * rng.uniform(0.97, 1.03), self._home_ip(c), None)
            tx.update({"counterparty": c.employer_iban, "counterparty_cc": "PS", "beneficiary_name": "Employer payroll",
                       "purpose": "Salary", "channel": "core_banking", "device": "", "amount": round(tx["amount"], 2)})
            out.append(tx)
        if c.remitter and day.day == 10 + (int(c.user_id[1:]) % 10):
            iban, cc, amount = c.remitter
            tx = self._base(c, self._time(day, c), "incoming_transfer", amount * rng.uniform(0.9, 1.1),
                            self._home_ip(c), None)
            tx.update({"counterparty": iban, "counterparty_cc": cc, "beneficiary_name": "Family remittance",
                       "purpose": "Family support", "channel": "swift_in", "device": "",
                       "amount": self._round_amount(tx["amount"])})
            out.append(tx)
        for _ in range(self._poisson(c.cheque_out_rate * factor)):
            out.append(self._cheque_payment(c, day + timedelta(hours=rng.uniform(8.5, 14.5))))
        for _ in range(self._poisson(c.cheque_in_rate * factor)):
            drawer = rng.choice(c.clients) if c.clients and rng.random() < 0.85 else ps_iban(rng)
            out.append(self._cheque_deposit(c, day + timedelta(hours=rng.uniform(8.5, 14.5)), drawer,
                                            str(rng.randint(100000, 999999))))
        for _ in range(self._poisson(c.receipts_rate * factor)):
            tx = self._base(c, self._time(day, c), "incoming_transfer",
                            min(rng.lognormvariate(c.amount_mu, c.amount_sigma), 40000.0), self._home_ip(c), None)
            tx.update({"counterparty": rng.choice(c.clients), "counterparty_cc": "PS", "purpose": "Customer payment",
                       "channel": "core_banking", "device": "", "amount": self._round_amount(tx["amount"])})
            out.append(tx)
        return out

    def _round_amount(self, amount: float) -> float:
        """People type round amounts: most transfers end in 0 / 50 / 100; some keep exact invoice values."""
        r = self.rng.random()
        if amount >= 1000 and r < 0.55:
            return float(round(amount / 100) * 100)
        if amount >= 100 and r < 0.7:
            return float(round(amount / 10) * 10)
        return round(amount, 2)

    def _poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        threshold, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= threshold:
                return k
            k += 1

    # ------------------------------------------------------------- fraud
    SCENARIOS = ("information_stealing", "transaction_hijacking", "salami_burst", "recharge_fraud", "prepaid_drain")
    SCENARIO_WEIGHTS = (35, 20, 20, 13, 12)
    PS_SCENARIOS = SCENARIOS + ("card_testing", "atm_cashout", "mule_passthrough", "cash_structuring",
                                "cheque_kiting", "duplicate_cheque", "stolen_chequebook")
    PS_WEIGHTS = (24, 14, 14, 8, 6, 12, 8, 9, 5, 4, 3, 3)

    def _mule_iban(self, country: Optional[str] = None) -> str:
        """Fraud payee: often one of the reused mule accounts, otherwise a fresh account."""
        rng = self.rng
        if rng.random() < 0.55:
            return rng.choice(self.mule_accounts)
        return make_iban(rng, country or rng.choice(FRAUD_IBAN_COUNTRIES))

    def make_fraud(self, c: Customer, ts: datetime, scenario: Optional[str] = None) -> list[dict]:
        rng = self.rng
        if scenario is None:
            scenario = rng.choices(self.PS_SCENARIOS, weights=self.PS_WEIGHTS)[0] if c.city else \
                rng.choices(self.SCENARIOS, weights=self.SCENARIO_WEIGHTS)[0]
        if scenario in ("card_testing", "atm_cashout", "mule_passthrough", "cash_structuring", "cheque_kiting",
                        "duplicate_cheque", "stolen_chequebook"):
            return self._ps_fraud(c, ts, scenario)
        med = self._median_amount(c)
        out = []
        if scenario == "information_stealing":
            ts = ts.replace(hour=rng.randint(0, 23), minute=rng.randint(0, 59))
            tx = self._base(c, ts, "bank_transfer", min(med * rng.uniform(5, 20), 24000.0) + rng.uniform(500, 3000),
                            self._foreign_ip(), scenario)
            tx["iban"] = self._mule_iban()
            out.append(tx)
        elif scenario == "transaction_hijacking":
            tx = self._base(c, ts, "bank_transfer", min(med * rng.uniform(3, 10), 14000.0) + rng.uniform(200, 1500),
                            self._home_ip(c), scenario)
            tx["iban"] = make_iban(rng, c.home) if rng.random() < 0.5 else self._mule_iban()
            out.append(tx)
        elif scenario == "salami_burst":
            ip = self._foreign_ip() if rng.random() < 0.5 else self._home_ip(c)
            for _ in range(rng.randint(4, 8)):
                tx = self._base(c, ts, "bank_transfer", med * rng.uniform(0.8, 2.5) + rng.uniform(50, 400), ip, scenario)
                tx["iban"] = make_iban(rng, rng.choice(FRAUD_IBAN_COUNTRIES + [c.home]))
                out.append(tx)
                ts += timedelta(minutes=rng.randint(3, 25))
        elif scenario == "recharge_fraud":
            ip = self._foreign_ip()
            for _ in range(rng.randint(4, 10)):
                tx = self._base(c, ts, "phone_recharge", rng.choice([50, 80, 100, 100, 120]), ip, scenario)
                tx["phone_number"] = f"+{rng.randint(20, 99)} {rng.randint(100000000, 999999999)}"
                tx["operator"] = rng.choice(["Unknown MVNO", "Lycamobile", "Lebara"])
                out.append(tx)
                ts += timedelta(minutes=rng.randint(1, 8))
        else:  # prepaid_drain
            ip = self._foreign_ip() if rng.random() < 0.7 else self._home_ip(c)
            card = f"4024 **** **** {rng.randint(1000, 9999)}"
            for _ in range(rng.randint(1, 3)):
                tx = self._base(c, ts, "prepaid_card", rng.uniform(800, 3000), ip, scenario)
                tx["card_id"] = card
                out.append(tx)
                ts += timedelta(minutes=rng.randint(2, 30))
        if c.city:
            for tx in out:
                if tx["tx_type"] == "bank_transfer":
                    tx["beneficiary_name"] = f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}" \
                        if tx["iban"].startswith("PS") else rng.choice(["Global Trade FZE", "M. Petrov", "Atlas Imports Ltd",
                                                                       "Nova Services SRL", "J. Smith", "Sunrise Holdings"])
                    tx["purpose"] = rng.choice(["Personal transfer", "Goods purchase", "Investment", "Family support"])
        return out

    def _cheque_payment(self, c: Customer, ts: datetime, number: Optional[int] = None, payee: str = "",
                        amount: Optional[float] = None, city: str = "", fraud: Optional[str] = None) -> dict:
        rng = self.rng
        if number is None:
            number = c.cheque_seq or 100000
            c.cheque_seq = number + 1
        tx = self._base(c, ts, "cheque_payment",
                        amount if amount is not None else self._round_amount(min(rng.lognormvariate(c.cheque_mu or 7.5, 0.5), 60000.0)),
                        self._home_ip(c), fraud)
        tx.update({"counterparty": payee or (rng.choice(c.payee_names) if c.payee_names else rng.choice(PS_COMPANIES)),
                   "cheque_number": str(number), "cheque_date": (ts - timedelta(days=rng.randint(0, 12))).date().isoformat(),
                   "location": city or c.city, "channel": "cheque_clearing", "device": "Branch counter",
                   "purpose": "Cheque payment"})
        return tx

    def _cheque_deposit(self, c: Customer, ts: datetime, drawer: str, number: str, amount: Optional[float] = None,
                        city: str = "", fraud: Optional[str] = None, cheque_date: Optional[str] = None) -> dict:
        rng = self.rng
        tx = self._base(c, ts, "cheque_deposit",
                        amount if amount is not None else self._round_amount(min(rng.lognormvariate(c.cheque_mu or 7.5, 0.55), 60000.0)),
                        self._home_ip(c), fraud)
        city = city or c.city
        tx.update({"counterparty": drawer, "drawer_bank": drawer[4:8] if drawer.startswith("PS") else drawer[:4],
                   "cheque_number": number,
                   "cheque_date": cheque_date or (ts - timedelta(days=rng.randint(0, 20))).date().isoformat(),
                   "location": city, "terminal": f"BR-{ATM_PREFIX.get(city, 'PS')}-01", "channel": "branch",
                   "device": "Branch counter", "purpose": "Cheque deposit"})
        return tx

    def _ps_fraud(self, c: Customer, ts: datetime, scenario: str) -> list[dict]:
        rng = self.rng
        out = []
        if scenario == "cheque_kiting":         # cheques from new drawers deposited, money taken out before clearing
            ts = ts.replace(hour=rng.randint(9, 11))
            total = 0.0
            for _ in range(rng.randint(2, 3)):
                amount = round(rng.uniform(6000, 15000), -1)
                out.append(self._cheque_deposit(c, ts, ps_iban(rng), str(rng.randint(100000, 999999)), amount,
                                                fraud=scenario))
                total += amount
                ts += timedelta(minutes=rng.randint(10, 40))
            ts += timedelta(hours=rng.uniform(1, 3))
            cash = round(total * rng.uniform(0.35, 0.5), -1)
            tx = self._base(c, ts, "cash_withdrawal", cash, self._home_ip(c), scenario)
            tx.update({"terminal": f"BR-{ATM_PREFIX.get(c.city, 'PS')}-01", "location": c.city, "channel": "branch",
                       "device": "Branch counter", "card_ref": c.card_ref})
            out.append(tx)
            tx = self._base(c, ts + timedelta(minutes=rng.randint(20, 90)), "bank_transfer",
                            round(total * rng.uniform(0.45, 0.55), 2), self._home_ip(c), scenario)
            tx["iban"] = self._mule_iban("PS")
            tx.update({"beneficiary_name": f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}", "purpose": "Personal transfer"})
            out.append(tx)
            return out
        if scenario == "duplicate_cheque":      # the same cheque presented twice (copy / mobile + branch)
            drawer, number = ps_iban(rng), str(rng.randint(100000, 999999))
            amount = round(rng.uniform(3000, 12000), -1)
            out.append(self._cheque_deposit(c, ts, drawer, number, amount, fraud=scenario))
            other = rng.choice([x for x in ATM_PREFIX if x != c.city])
            out.append(self._cheque_deposit(c, ts + timedelta(hours=rng.uniform(1, 5)), drawer, number, amount,
                                            city=other, fraud=scenario))
            return out
        if scenario == "stolen_chequebook":     # cheques far outside the customer's sequence, unknown payees
            start = (c.cheque_seq or 100000) + rng.randint(3000, 9000)
            city = rng.choice([x for x in ATM_PREFIX if x != c.city])
            for k in range(rng.randint(2, 3)):
                out.append(self._cheque_payment(c, ts + timedelta(hours=k), number=start + k,
                                                payee=f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}",
                                                amount=round(rng.uniform(5000, 18000), -1), city=city, fraud=scenario))
            return out
        if scenario == "card_testing":          # stolen card details tested with tiny online payments, then cashed out
            ip = self._foreign_ip()
            name, mcc, cc = rng.choice(FRAUD_MERCHANTS)
            for k in range(rng.randint(3, 6)):
                amount = round(rng.uniform(1, 6), 2) if k < 3 else round(rng.uniform(600, 2500), 2)
                tx = self._base(c, ts, "card_payment", amount, ip, scenario)
                tx.update({"merchant": name, "mcc": mcc, "merchant_cc": cc, "entry_mode": "ECOM",
                           "channel": "card_ecom", "device": "Unknown device"})
                out.append(tx)
                ts += timedelta(minutes=rng.randint(1, 6))
        elif scenario == "atm_cashout":         # cloned card emptied at ATMs far from home, at night
            ts = ts.replace(hour=rng.choice([0, 1, 2, 3, 23]))
            for _ in range(rng.randint(3, 5)):
                city = rng.choice([x for x in ATM_PREFIX if x != c.city])
                tx = self._base(c, ts, "cash_withdrawal", 2000.0, self._home_ip(c), scenario)
                tx.update({"terminal": f"ATM-{ATM_PREFIX[city]}-{rng.randint(30, 60):03d}", "location": city,
                           "channel": "atm", "device": "ATM / kiosk"})
                out.append(tx)
                ts += timedelta(minutes=rng.randint(4, 15))
        elif scenario == "mule_passthrough":    # account used as a mule: several unknown senders, money leaves abroad
            total = 0.0
            for _ in range(rng.randint(3, 5)):
                amount = round(rng.uniform(1500, 4500), -1)
                tx = self._base(c, ts, "incoming_transfer", amount, self._home_ip(c), scenario)
                tx.update({"counterparty": ps_iban(rng), "counterparty_cc": "PS", "purpose": "Personal transfer",
                           "channel": "core_banking", "device": ""})
                out.append(tx)
                total += amount
                ts += timedelta(minutes=rng.randint(20, 120))
            tx = self._base(c, ts + timedelta(hours=rng.uniform(1, 6)), "bank_transfer", round(total * 0.95, 2),
                            self._foreign_ip() if rng.random() < 0.5 else self._home_ip(c), scenario)
            tx["iban"] = self._mule_iban()
            tx.update({"beneficiary_name": rng.choice(["Global Trade FZE", "Atlas Imports Ltd", "M. Petrov",
                                                       "Nova Services SRL", "Sunrise Holdings"]),
                       "purpose": "Investment"})
            out.append(tx)
        else:                                   # cash_structuring: several deposits just below 10,000
            ts = ts.replace(hour=9)
            for _ in range(rng.randint(3, 4)):
                tx = self._base(c, ts, "cash_deposit", round(rng.uniform(8600, 9950), -1), self._home_ip(c), scenario)
                tx.update({"terminal": f"BR-{ATM_PREFIX.get(c.city, 'PS')}-0{rng.randint(1, 3)}",
                           "location": c.city, "channel": "branch", "device": "Branch counter"})
                out.append(tx)
                ts += timedelta(minutes=rng.randint(40, 120))
        return out

    # ------------------------------------------------------------ simulation
    def simulate_day(self, day: datetime, fraud_ratio: Optional[float] = None) -> list[dict]:
        ratio = self.fraud_ratio if fraud_ratio is None else fraud_ratio
        out = []
        for c in self.customers:
            out.extend(self._legit_day(c, day))
        if ratio > 0 and out:
            expected_events = len(out) * ratio / (1 - ratio) / 2.2   # ~2.2 tx per fraud event
            for _ in range(self._poisson(expected_events)):
                victim = self.rng.choice(self.customers)
                start = day + timedelta(hours=self.rng.uniform(7, 22))
                out.extend(self.make_fraud(victim, start))
        out.sort(key=lambda r: r["timestamp"])
        return out

    def history(self, days: int, fraud_ratio: float = 0.0, end: Optional[datetime] = None) -> list[dict]:
        end = (end or self.cursor).replace(hour=0, minute=0, second=0, microsecond=0)
        day = end - timedelta(days=days)
        records = []
        while day < end:
            records.extend(self.simulate_day(day, fraud_ratio))
            day += timedelta(days=1)
        self.cursor = end
        return records

    def live(self, fraud_ratio: Optional[float] = None) -> Iterator[dict]:
        """Infinite stream continuing the simulation from :attr:`cursor` (simulated clock)."""
        while True:
            for record in self.simulate_day(self.cursor, fraud_ratio):
                yield record
            self.cursor += timedelta(days=1)
