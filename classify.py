"""
classify.py — Can Jazz deliver this?

THE REFRAME
    The wrong question is "is this digital?". That returns laptops, projectors
    and CCTV, none of which Jazz can bid. The right question is "can Jazz
    deliver this?" Every tender is sorted into one of three lanes.

    CORE     Jazz can bid today, largely alone. Connectivity, IoT/M2M,
             messaging, digital financial services, cloud and hosting.
    PARTNER  Jazz leads a consortium or supplies the network spine while a
             partner supplies the rest. Safe city, managed security, SI.
    SIGNAL   Not biddable, but tells you an institution has digitisation
             budget and intent. Account intelligence, kept out of the
             opportunity feed on purpose.
    NONE     Irrelevant. Civil works, vehicles, land, stationery.

    Each tender also carries the Jazz PRODUCT LINE it maps to, so the feed
    answers "which part of the business owns this" rather than just naming a
    topic. Every decision stores the keywords that produced it, so any tag
    can be audited and argued with.
"""

import re

# ── CORE: Jazz can bid essentially alone. Keyed by product line.
CORE = {
    "Connectivity": [
        "fiber optic", "fibre optic", "fiber", "fibre", "ftth", "fttx", "gpon", "epon",
        "dark fib", "leased line", "last mile", "backhaul", "backbone network",
        "bandwidth", "internet service", "internet connectivity", "internet bandwidth",
        "broadband", "mpls", "sd-wan", "sdwan", "vpn service", "wide area network",
        "wan connectivity", "vsat", "satellite link", "microwave link",
        "network connectivity", "connectivity service", "data link", "p2p link",
        "satellite data service", "marlink", "vsat bandwidth", "campus lan",
        "lan solution", "wireless lan", "wlan solution", "layer 2 switch",
        "layer ii switch", "network switch", "core switch", "network equipment",
        "oem backed support", "telephone exchange", "pabx system",
        "point to point link", "metro ethernet", "dedicated internet",
        "network rollout", "network expansion", "right of way", "duct",
    ],
    "Enterprise Mobility": [
        "sim card", "e-sim", "esim", "corporate sim", "bulk sim", "mobile subscription",
        "cellular service", "mobile service", "voice service", "airtime",
        "closed user group", "cug", "mobile connection", "postpaid connection",
        "roaming service", "private lte", "private network", "spectrum",
        "base station", "bts", "in-building solution", "ibs", "tower colocation",
    ],
    "IoT & M2M": [
        "iot", "internet of things", "m2m", "machine to machine", "telemetry",
        "telemetric", "smart meter", "ami system", "amr system", "meter data",
        "sensor network", "remote monitoring", "asset tracking", "vehicle tracking",
        "fleet management", "gps tracking", "smart water", "smart grid communication",
        "scada communication", "rtu communication",
    ],
    "Messaging & CX": [
        "bulk sms", "sms gateway", "sms service", "sms platform", "short code",
        "ussd", "ivr", "voice broadcast", "call cent", "contact cent", "helpline",
        "notification platform", "citizen notification", "alert system",
        "contact centre option", "contact center option", "call centre solution",
        "whatsapp bot", "whatsapp business", "conversational ai",
        "whatsapp business", "chatbot service", "omnichannel",
    ],
    "Digital Financial Services": [
        "mobile wallet", "mobile money", "branchless banking", "payment gateway",
        "e-payment", "digital payment", "payment collection", "fee collection",
        "g2p", "disbursement", "stipend disbursement", "cash transfer",
        "e-kyc", "biometric verification service", "financial inclusion",
        "merchant acquiring", "bill payment", "salary disbursement",
    ],
    "Cloud & Hosting": [
        "cloud service", "cloud subscription", "cloud hosting", "cloud migration",
        "iaas", "paas", "public cloud", "private cloud", "colocation", "co-location",
        "data cent", "datacent", "disaster recovery site", "dr site", "hosting service",
        "managed hosting", "virtual machine", "cloud storage", "backup as a service",
        "rack mount server", "rack server", "blade server", "san storage",
        "storage area network", "nas storage", "hyper-converged", "hyperconverged",
        "hci solution", "server virtualization", "server virtualisation",
    ],
}

# ── PARTNER: Jazz leads a consortium or owns the network layer.
PARTNER = {
    "Managed Security": [
        "security operation cent", "soc service", "siem", "soar", "managed security",
        "cyber security service", "cybersecurity service", "threat intelligence",
        "ddos protection", "anti-ddos", "penetration test", "vapt",
        "vulnerability assessment", "incident response", "security monitoring",
        "endpoint detection", "zero trust", "network security",
        "endpoint security", "edr solution", "xdr solution", "ngfw",
        "fortigate", "firewall rental", "web application firewall",
    ],
    "Smart City & Surveillance": [
        "safe city", "smart city", "command and control cent", "command centre",
        "integrated traffic", "surveillance network", "cctv network",
        "video management system", "anpr", "number plate recognition",
        "emergency response system", "911 system", "1122 system",
    ],
    "Systems Integration": [
        "system integration", "systems integration", "turnkey solution",
        "end to end solution", "digital transformation", "digitiz", "digitis",
        "automation of", "e-office", "e-governance", "e-government",
        "management information system", "enterprise resource planning",
        "erp implementation", "workflow automation", "process automation",
        "case management system", "billing system", "revenue management system",
    ],
    "Data & Analytics": [
        "data analytic", "business intelligence", "data warehouse", "data lake",
        "artificial intelligence", "machine learning", "predictive analytic",
        "big data", "dashboard development", "gis system", "geographic information",
        "ai capacity", "ai based", "ai solution", "ai platform",
        "artificial intelligence hub", "ai hub", "production monitoring",
        "track and trace", "monitoring solution", "data centre solution",
    ],
}

# ── SIGNAL: not biddable, but shows digitisation budget and intent.
SIGNAL = {
    "IT Procurement": [
        "laptop", "desktop computer", "notebook computer", "workstation", "tablet",
        "printer", "scanner", "photocopier", "multifunction device", "toner",
        "computer hardware", "it equipment", "ict equipment", "peripheral",
        "projector", "multimedia", "smart board", "interactive board", "led display",
        "video wall", "ups system", "server hardware", "storage hardware",
        "router", "switch", "access point", "firewall appliance", "structured cabling",
    ],
    "Software Licensing": [
        "software licen", "microsoft licen", "office 365", "microsoft 365",
        "oracle licen", "sap licen", "antivirus", "annual technical support",
        "ats renewal", "subscription renewal", "software maintenance",
        "application development", "web portal", "website development", "mobile app",
        "software development", "database licen",
    ],
    "IT Services": [
        "it support", "it services", "helpdesk", "service desk", "manpower for it",
        "outsourcing of it", "technical staff", "software house", "consultancy for it",
        "feasibility study", "it audit", "training", "capacity building",
    ],
}

# Buyers that make even a vague tender worth watching. Digitisation budget holders.
DIGITAL_BUYERS = [
    "information technology", "it and telecom", "it & telecom", "telecommunication",
    "national telecommunication", "universal service fund", "pakistan digital",
    "frequency allocation", "ignite", "national technology fund",
    "national information technology board", "software export", "nadra",
    "national database", "special communication", "revenue automation",
    "punjab information technology", "sindh information technology",
    "khyber pakhtunkhwa information technology", "e-governance", "national center for cyber",
]

# Named accounts. Elevate priority regardless of lane.
KEY_ACCOUNTS = [
    "national telecommunication", "universal service fund", "it and telecom",
    "pakistan telecommunication authority", "frequency allocation", "pakistan digital",
    "nadra", "national database", "revenue automation", "punjab information technology",
    "special communication", "national information technology board", "ignite",
    "federal board of revenue", "state bank", "benazir income support",
    "national highway authority", "wapda", "sui northern", "sui southern",
]

# Hard exclusions. If these dominate, it is not an opportunity whatever else matches.
EXCLUDE = [
    "boundary wall", "civil work", "construction of building", "road repair",
    "furniture", "stationery", "vehicle purchase", "fuel supply", "pol supply",
    "medicine", "surgical", "uniform", "catering", "horticulture", "land acquisition",
    "tuff tile", "sewerage", "drainage", "plumbing", "air conditioning plant",
    "transformer", "grid station", "transmission line", "switchgear", "conductor",
    "insurance", "audit firm", "legal advisor", "security guard", "janitorial",
    "sanitation", "generator", "solarization", "solar panel", "street light",
]

# Context guards: kill a keyword when it appears in a non-Jazz setting.
NEG_CONTEXT = [
    (r"switch|router|cable|tower|network",
     r"transmission line|grid station|\d+\s*kv|switchgear|circuit breaker|"
     r"electric|power cable|conductor|pylon|cooling tower|water tower"),
    (r"training|capacity building", r"teacher|nurse|farmer|driver"),
]


# ── Vocabulary expansion, validated against the team's curated gold set.
# These terms were missing and caused real Jazz-relevant tenders to drop to
# None. Kept specific enough not to pull in civil or physical-goods tenders.
CORE["Connectivity"] += [
    "internet link", "backup internet", "backup link", "provision of internet",
    "internet services", "backhaul link", "managed connectivity",
]
CORE["IoT & M2M"] += [
    "tracker", "gps tracker", "vehicle tracker", "telematics",
    "tracking and management", "tracking system", "tracking service",
]
CORE["Cloud & Hosting"] += [
    "datacenter", "data center", "tier-iii", "tier iii", "tier-3 data",
    "tier iii data", "certified datacenter", "compute server",
    "high performance compute", "hpc solution", "storage server",
]
PARTNER["Managed Security"] += [
    "ng firewall", "next generation firewall", "next-generation firewall",
    "endpoint protection", "firewall license", "firewall licence",
    "firewall renewal", "sd-wan enabled", "wireless access point",
]
PARTNER["Smart City & Surveillance"] += [
    "cctv", "cctv surveillance", "surveillance solution", "surveillance system",
    "video surveillance", "public announcement system", "pa system",
]
PARTNER["Systems Integration"] += [
    "management system", "campus management system", "learning management system",
    "examination management system", "education management system",
    "voice communication system", "communication system", "web-based",
    "web based", "monitoring dashboard", "project monitoring", "erp",
    "microplanning", "gis integration",
]
PARTNER["Data & Analytics"] += [
    "gis", "gis-enabled", "gis enabled", "ai cloud", "government ai",
    "ai-powered", "ai powered", "ai-enabled", "ai enabled",
]
SIGNAL["IT Procurement"] += [
    "layer-2 switch", "layer 2 switch", "manageable switch", "ssd", "pdu",
    "cat-06", "cat 6 cable", "network infrastructure", "networking infrastructure",
    "computer lab", "computer labs", "gpu", "gpu server", "server",
    "data server", "rack server", "blade server", "server hardware",
    "networking items", "networking item", "it equipment", "ict equipment",
]
# A second field pass. Terms that were still dropping real ICT leads.
PARTNER["Systems Integration"] += [
    "software", "software license", "software licence", "license renewal",
    "licence renewal", "website", "web development", "website development",
    "web portal", "web application", "call center", "call centre",
    "call center agent", "document management system", "dms", "digitization",
    "digitisation", "computer labs", "windows 11", "ms office", "microsoft office",
    "microsoft license", "cisco", "electronic data management",
]
CORE["Connectivity"] += [
    "wi-fi", "wifi", "wlan", "lan setup", "wireless lan", "ddos", "ddos protection",
    "website hosting", "san storage",
]


def _c(words):
    return [(w, re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"s?(?![a-z])", re.I))
            for w in words]


_CORE = {k: _c(v) for k, v in CORE.items()}
_PARTNER = {k: _c(v) for k, v in PARTNER.items()}
_SIGNAL = {k: _c(v) for k, v in SIGNAL.items()}
_EXCL = _c(EXCLUDE)
_NEG = [(re.compile(a, re.I), re.compile(b, re.I)) for a, b in NEG_CONTEXT]
_BUYER = {k: re.compile(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", re.I)
          for k in DIGITAL_BUYERS}
_ACCT = {k: re.compile(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", re.I)
         for k in KEY_ACCOUNTS}

LANES = ["Core", "Partner-led", "Signal"]
PRODUCT_LINES = list(CORE) + list(PARTNER) + list(SIGNAL)


def _suppressed(term, text):
    for trig, ctx in _NEG:
        if trig.search(term) and ctx.search(text):
            return True
    return False


def _hits(groups, text):
    out = {}
    for line, pairs in groups.items():
        found = [w for w, rx in pairs if rx.search(text) and not _suppressed(w, text)]
        if found:
            out[line] = found
    return out


def classify(title, description="", buyer="", sector=""):
    """Return lane, product line, evidence, relevance, rationale."""
    text = " " + " ".join(str(x or "") for x in (title, description, sector)).lower() + " "
    b = " " + str(buyer or "").lower() + " "

    excl = [w for w, rx in _EXCL if rx.search(text)]
    core = _hits(_CORE, text)
    partner = _hits(_PARTNER, text)
    signal = _hits(_SIGNAL, text)

    account = next((k for k in KEY_ACCOUNTS if _ACCT[k].search(b)), None)
    dbuyer = next((k for k in DIGITAL_BUYERS if _BUYER[k].search(b)), None)

    # Exclusions win only when nothing digital is present. A Signal-level hit,
    # for example an IT-equipment lot inside a mixed prequalification, survives
    # as a watch item rather than being dropped entirely.
    if excl and not core and not partner and not signal:
        return _out("None", "", "", "Low",
                    f"excluded: {', '.join(excl[:3])}", 0)

    if core:
        lane = "Core"
        line = max(core, key=lambda k: len(core[k]))
        ev = "; ".join(f"{k}: {', '.join(v[:4])}" for k, v in core.items())
        rel = "High"
        why = f"Jazz can bid directly. {line} keywords: {', '.join(core[line][:3])}"
        if account:
            why += f". Key account: {account}"
        score = 90 + min(9, len(core[line]))
    elif partner:
        lane = "Partner-led"
        line = max(partner, key=lambda k: len(partner[k]))
        ev = "; ".join(f"{k}: {', '.join(v[:4])}" for k, v in partner.items())
        rel = "High" if account else "Medium"
        why = f"Requires a partner or consortium. {line} keywords: {', '.join(partner[line][:3])}"
        if account:
            why += f". Key account: {account}"
        score = 60 + min(15, len(partner[line]) * 3) + (10 if account else 0)
    elif signal:
        lane = "Signal"
        line = max(signal, key=lambda k: len(signal[k]))
        ev = "; ".join(f"{k}: {', '.join(v[:4])}" for k, v in signal.items())
        rel = "Medium" if (account or dbuyer) else "Low"
        why = (f"Not biddable by Jazz. Indicates digitisation spend"
               f"{' at a key account: ' + account if account else ''}")
        score = 25 + (15 if account else 0)
    elif dbuyer:
        lane = "Signal"
        line = "IT Services"
        ev = f"digitisation buyer: {dbuyer}"
        rel = "Medium" if account else "Low"
        why = f"Buyer is a digitisation budget holder ({dbuyer}), scope unclear from title"
        score = 20
    else:
        return _out("None", "", "", "Low", "", 0)

    return _out(lane, line, ev, rel, why, score)


def _out(lane, line, ev, rel, why, score):
    return {
        "lane": lane,
        "product_line": line,
        "is_digital": int(lane != "None"),
        "is_opportunity": int(lane in ("Core", "Partner-led")),
        "digital_domain": line,
        "digital_evidence": ev[:500],
        "relevance": rel,
        "rationale": why[:400],
        "fit_score": score,
    }


def lanes():
    return LANES


def product_lines():
    return PRODUCT_LINES
