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
    "Voice & UC": [
        "sip trunk", "sip trunking", "toll free", "uan", "pri line", "pri", "e1 line", "e1 lines",
        "ip telephony", "voip", "voice services", "voice gateway", "ip pbx", "ippbx", "pabx",
        "telephone exchange", "hosted pbx", "unified communication", "teams calling", "landline",
    ],
    "Connectivity": [
        "intranet link", "internet link", "data link", "link between", "wan link", "ipv4", "ipv6", "ip address", "asn", "apnic", "ip transit", "dedicated internet access", "dia", "iplc", "ofc", "optical fiber", "optical fibre", "microwave radio", "radio link", "multiplexer", "fmx", "msap", "sdh", "infinet", "point to multipoint", "wifi", "wi-fi", "isp", "internet service provider", "internet services", "internet connection", "cir", "lte", "4g", "5g", "wireless data", "data services", "data connection", "mobile data", "mifi", "dongle", "data sim", "sim based", "satellite phone", "satellite internet", "bgan", "thuraya", "inmarsat", "starlink",
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
        "cellular services", "cellular connections", "mobile connections", "gsm", "push to talk", "ptt",
        "sim card", "e-sim", "esim", "corporate sim", "bulk sim", "mobile subscription",
        "cellular service", "mobile service", "voice service", "airtime",
        "closed user group", "cug", "mobile connection", "postpaid connection",
        "roaming service", "private lte", "private network", "spectrum",
        "base station", "bts", "in-building solution", "ibs", "tower colocation",
    ],
    "IoT & M2M": [
        "tracker", "trackers", "tracking device", "vehicle tracking", "avl", "rtu", "remote terminal unit", "communication gateway", "smart metering", "metering", "sensor", "sensors", "early warning system", "lora", "nb-iot", "nbiot", "geo-fencing", "geofencing",
        "iot", "internet of things", "m2m", "machine to machine", "telemetry",
        "telemetric", "smart meter", "ami system", "amr system", "meter data",
        "sensor network", "remote monitoring", "asset tracking", "vehicle tracking",
        "fleet management", "gps tracking", "smart water", "smart grid communication",
        "scada communication", "rtu communication",
    ],
    "Messaging & CX": [
        "sms", "short code", "whatsapp", "chatbot", "bot", "call center", "call centre", "contact center", "contact centre", "helpdesk services", "a2p",
        "bulk sms", "sms gateway", "sms service", "sms platform", "short code",
        "ussd", "ivr", "voice broadcast", "call cent", "contact cent", "helpline",
        "notification platform", "citizen notification", "alert system",
        "contact centre option", "contact center option", "call centre solution",
        "whatsapp bot", "whatsapp business", "conversational ai",
        "whatsapp business", "chatbot service", "omnichannel",
    ],
    "Digital Financial Services": [
        "pos terminal", "qr payment", "qr code payment", "collection system", "online fee", "e-challan", "challan", "payment solution", "payment system", "wallet", "jazzcash", "easypaisa", "raast", "ibft", "loan origination", "lending", "credit scoring", "microfinance solution", "core banking", "digital banking", "e-commerce", "ecommerce", "marketplace",
        "mobile wallet", "mobile money", "branchless banking", "payment gateway",
        "e-payment", "digital payment", "payment collection", "fee collection",
        "g2p", "disbursement", "stipend disbursement", "cash transfer",
        "e-kyc", "biometric verification service", "financial inclusion",
        "merchant acquiring", "bill payment", "salary disbursement",
    ],
    "Cloud & Hosting": [
        "dns", "managed dns", "gpu", "cloud infrastructure", "web hosting", "website hosting", "email hosting", "data center", "data centre", "tier iii", "dr site", "disaster recovery", "backup solution", "object storage", "sovereign cloud",
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
        "firewall", "next generation firewall", "detection and response", "extended detection", "ndr", "xdr", "edr", "forti", "fortinet", "forti token", "network access control", "nac", "vpn", "endpoint", "waf", "dlp", "email security", "pam", "security equipment", "cyber", "fraud risk", "fraud management", "efrm", "anti-fraud", "security audit", "penetration", "ddos",
        "security operation cent", "security operations cent", "soc service", "siem", "soar", "managed security",
        "cyber security service", "cybersecurity service", "threat intelligence",
        "ddos protection", "anti-ddos", "penetration test", "vapt",
        "vulnerability assessment", "incident response", "security monitoring",
        "endpoint detection", "zero trust", "network security",
        "endpoint security", "edr solution", "xdr solution", "ngfw",
        "fortigate", "firewall rental", "web application firewall",
    ],
    "Smart City & Surveillance": [
        "smart surveillance", "access control", "cctv", "surveillance system", "integrated surveillance", "video analytics", "camera analytics", "anpr", "traffic management", "command center",
        "safe city", "smart city", "command and control cent", "command centre",
        "integrated traffic", "surveillance network", "cctv network",
        "video management system", "anpr", "number plate recognition",
        "emergency response system", "911 system", "1122 system",
    ],
    "Systems Integration": [
        "information system", "hmis", "hrmis", "lmis", "pmis", "erp", "software solution", "software development", "examination system", "computer based examination", "cbe", "e-office", "portal", "mobile application", "turnkey", "network equipment", "integration", "implementation", "lan solution", "wireless lan", "structured network", "video conferencing", "video conference", "telepresence", "conference solution", "dashboard visualization", "video wall", "vdi", "virtual desktop", "next generation technologies", "technology complex", "ar/vr", "augmented reality", "virtual reality", "projection mapping", "production monitoring", "production counting",
        "system integration", "systems integration", "turnkey solution",
        "end to end solution", "digital transformation", "digitiz", "digitis",
        "automation of", "e-office", "e-governance", "e-government",
        "management information system", "enterprise resource planning",
        "erp implementation", "workflow automation", "process automation",
        "case management system", "billing system", "revenue management system",
    ],
    "Data & Analytics": [
        "data management", "monitoring hub", "insights", "discourse analysis", "trend analysis", "analytics", "ai enabled", "ai-enabled", "artificial intelligence", "ai infrastructure", "sovereign ai", "nlp", "ocr", "machine learning", "big data", "data warehouse", "business intelligence",
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
        "managed printing", "copying and scanning", "database appliance", "tape library", "storage", "desktop", "desktops", "computer", "computers", "ip phone", "ip phones", "it equipment", "hardware", "scanners", "biometric devices", "tablets", "led", "smd screen", "interactive screen",
        "laptop", "desktop computer", "notebook computer", "workstation", "tablet",
        "printer", "scanner", "photocopier", "multifunction device", "toner",
        "computer hardware", "it equipment", "ict equipment", "peripheral",
        "projector", "multimedia", "smart board", "interactive board", "led display",
        "video wall", "ups system", "server hardware", "storage hardware",
        "router", "switch", "access point", "firewall appliance", "structured cabling",
    ],
    "Software Licensing": [
        "oracle", "microsoft", "office 365", "licenses", "licences", "license renewal", "veeam", "sap hardware", "support renewal",
        "software licen", "microsoft licen", "office 365", "microsoft 365",
        "oracle licen", "sap licen", "antivirus", "annual technical support",
        "ats renewal", "subscription renewal", "software maintenance",
        "application development", "web portal", "website development", "mobile app",
        "software development", "database licen",
    ],
    "IT Services": [
        "technical audit", "software audit", "cms", "it support", "annual maintenance",
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
    # duct lines for HT/LT power cables and civil works are not telecom
    (r"duct|right of way|network expansion",
     r"construction of|civil work|ht/lt|\d+\s*kv|shifting of|pcc|flooring|"
     r"excavat|trench|manhole|drain|sewer|road cut|boundary wall"),
    # fibre that is a material or a laser, not a network
    (r"fiber|fibre", r"laser|marking machine|fiber channel|fibre channel|"
     r"fiber glass|fibreglass|fiberglass|glass sheet|fiber shed|reinforced|"
     r"cement|textile|cloth"),
]


# Terms whose final token is a word STEM rather than a whole word. Without
# this, "call cent" could never match "call centre", because the trailing
# boundary rejected the letters that follow. That silently excluded call
# centres, data centres and SOCs from the Core and Partner lanes.
STEMS = {"cent", "datacent", "digitiz", "digitis", "licen", "analytic",
         "virtualiz", "virtualis", "automat", "telemetr"}


def _c(words):
    out = []
    for w in words:
        tail = r"[a-z]*" if w.split()[-1] in STEMS else r"s?(?![a-z])"
        out.append((w, re.compile(r"(?<![a-z0-9])" + re.escape(w) + tail, re.I)))
    return out


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


CLASSIFIER_VERSION = "6.0"

# ── second-order dimensions used by analytics ─────────────────────────
CITIES = ["islamabad", "rawalpindi", "karachi", "lahore", "peshawar", "quetta",
          "faisalabad", "multan", "hyderabad", "sukkur", "gujranwala", "sialkot",
          "bahawalpur", "sargodha", "abbottabad", "mardan", "gilgit", "skardu",
          "muzaffarabad", "gwadar", "sahiwal", "taxila", "wah cantt", "wah",
          "kohat", "swat", "murree", "larkana", "dera ismail khan", "d i khan",
          "jhelum", "sheikhupura", "okara", "rahim yar khan", "nowshera",
          "chitral", "mirpur", "haripur", "attock", "chakwal", "mianwali",
          "kasur", "jhang", "dera ghazi khan", "dg khan", "jamshoro", "nawabshah",
          "mirpurkhas", "thatta", "turbat", "khuzdar", "bannu", "risalpur"]

PARENTS = ["Banking Mohtasib Pakistan", "Federal Board of Revenue (FBR)",
           "Higher Education Commission (HEC)", "Ministry of Climate Change",
           "Ministry of Commerce", "Ministry of Communications", "Ministry of Defence",
           "Ministry of Energy (Petroleum Division)", "Ministry of Energy (Power Division)",
           "Ministry of Finance", "Ministry of Foreign Affairs",
           "Ministry of IT and TeleCommunication", "Ministry of IT and Telecom",
           "Ministry of Industries & Production (MoIP)",
           "Ministry of Interior and Narcotics Control", "Ministry of Law and Justice",
           "Ministry of National Food Security & Research (MNFSR)",
           "Ministry of National Health Services, Regulation & Coordination",
           "Ministry of Planning, Development & Special Initiatives",
           "Ministry of Federal Education and Professional Training",
           "Ministry of Information and Broadcasting", "Ministry of Railways",
           "Ministry of Maritime Affairs", "Ministry of Water Resources",
           "Cabinet Division", "National Heritage & Culture Division",
           "Pakistan Atomic Energy Commission (PAEC)"]
_PARENTS = sorted(PARENTS, key=len, reverse=True)


def normalize_buyer(buyer):
    """Reduce an EPMS buyer string to the organisation that actually buys.

    EPMS concatenates parent ministry, organisation and city, often with the
    organisation repeated: "Islamabad Electric Supply Company (IESCO)
    Islamabad Electric Supply Company (IESCO) Islamabad - Pakistan". Raw
    strings split one account into many rows, which is why the contacts and
    account views under-counted every buyer.
    """
    b = re.sub(r"\s+", " ", str(buyer or "")).strip()
    if not b:
        return ""
    b = re.sub(r"[\s,-]+pakistan\s*$", "", b, flags=re.I).strip()
    for _ in range(3):
        low = b.lower()
        hit = next((c for c in CITIES if low.endswith(" " + c)), None)
        if not hit:
            break
        b = b[:-(len(hit) + 1)].strip(" ,-")
    for p in _PARENTS:
        if b.lower().startswith(p.lower()):
            rest = b[len(p):].strip(" ,-")
            acr = re.search(r"\(([A-Za-z]{2,8})\)", p)
            own_office = acr and rest.lower().startswith(acr.group(1).lower())
            if len(rest.split()) >= 2 and not own_office:
                b = rest
            elif own_office:
                b = p
            break
    w = b.split()
    for k in range(len(w) // 2, 1, -1):
        if w[:k] == w[k:2 * k]:
            w = w[:k] + w[2 * k:]
            break
    b = " ".join(w)
    m = re.match(r"^(.*\(([A-Z]{2,8})\))\s+\2\b(.*)$", b)
    if m:
        b = (m.group(1) + m.group(3)).strip()
    return b[:160]


SECTORS = [
    ("Multilateral & Donor", r"world bank|asian development|\badb\b|undp|unicef|"
                             r"\bwfp\b|usaid|ifad|\bun\b agency|giz|jica"),
    ("Telecom & IT", r"telecom|information technology|\bnitb\b|\bpta\b|\bntc\b|"
                     r"universal service|\busf\b|ignite|\bpseb\b|\bpitb\b|nadra|"
                     r"national database|\bpral\b|revenue automation|special communication|"
                     r"\bsco\b|frequency allocation|digital|e-?governance|cyber"),
    ("Finance & Revenue", r"finance|revenue|\bfbr\b|\bbank\b|state bank|\bsecp\b|"
                          r"securities|insurance|state life|\bnicl\b|monitoring unit|"
                          r"\bfmu\b|mint|microfinance|accountant general"),
    ("Energy & Utilities", r"energy|power|electric|wapda|\bngc\b|grid|gas|sngpl|ssgc|"
                           r"ogdc|oil|petroleum|\bpso\b|\bppl\b|hydel|iesco|lesco|fesco|"
                           r"gepco|mepco|pesco|hesco|qesco|sepco|tesco|ntdc|\bpitc\b|"
                           r"water and sanitation|wasa|atomic energy"),
    ("Defence & Security", r"defence|army|navy|air force|\bpaf\b|police|rangers|"
                           r"frontier corps|interior|\bfia\b|ordnance|\bpof\b|"
                           r"heavy industries|military|signals|cantonment|counter narcotics|"
                           r"protection unit|emergency services|rescue|disaster management|"
                           r"\bndma\b|ndrmf"),
    ("Health", r"health|hospital|medical|cardiology|\bnih\b|drug regulatory|drap|"
               r"pharma|pphi|neonatology|population"),
    ("Education & Research", r"universit|college|education|school|\bhec\b|"
                             r"institute of|research|pcrwr|navttc|academy|\biba\b|"
                             r"\bnust\b|comsats"),
    ("Transport & Infrastructure", r"railway|highway|\bnha\b|airport|aviation|\bpia\b|"
                                   r"\bports?\b|shipping|ministry of communications|motorway|ring road|"
                                   r"infrastructure development|works department|"
                                   r"road asset|transport"),
    ("Governance & Public Admin", r"cabinet|secretariat|ombudsman|mohtasib|court|law|"
                                  r"justice|parliament|election|accountability|planning|"
                                  r"statistics|\bnipa\b|foreign affairs|commerce|"
                                  r"information and broadcasting|press information"),
    ("Provincial Government", r"government of (?:the )?(?:punjab|sindh|khyber|balochistan)|"
                              r"punjab|sindh|khyber pakhtunkhwa|balochistan|district"),
]
_SECTORS = [(n, re.compile(p, re.I)) for n, p in SECTORS]


def buyer_sector(buyer):
    b = str(buyer or "")
    for name, rx in _SECTORS:
        if rx.search(b):
            return name
    return "Other Public Sector"


TENDER_TYPES = [
    ("EOI / Prequalification", r"expression of interest|\beoi\b|pre-?\s?qualification|"
                               r"prequalification|enlistment|registration of firms"),
    ("Framework agreement", r"framework"),
    ("Consultancy", r"consultan|third party|\btpv\b|\btpm\b|feasibility|advisory|"
                    r"audit firm|technical audit"),
    ("Solution / turnkey", r"turnkey|establishment of|implementation|deployment|"
                           r"development of|design,? development|digiti[sz]|"
                           r"end[- ]to[- ]end|integration of|system for|\bsystem\b|solution|platform"),
    ("Works", r"construction|civil work|renovation|repair work|rehabilitation|"
              r"laying of|erection"),
    ("Services", r"hiring|provision of|services?\b|outsourcing|maintenance|support|managed|"
                 r"cent(?:re|er)\b|"
                 r"subscription|\bamc\b|rental|lease|renewal"),
    ("Supply", r"supply|procurement|purchase|acquisition|provide"),
]
_TYPES = [(n, re.compile(p, re.I)) for n, p in TENDER_TYPES]


def tender_type(title, description=""):
    t = f"{title or ''} {description or ''}"
    for name, rx in _TYPES:
        if rx.search(t):
            return name
    return "Other"


def classify(title, description="", buyer="", sector=""):
    """Lane classification plus the dimensions analytics groups by."""
    out = _classify_lane(title, description, buyer, sector)
    nb = normalize_buyer(buyer)
    out["buyer_norm"] = nb
    out["buyer_sector"] = buyer_sector(f"{buyer} {nb}")
    out["tender_type"] = tender_type(title, description)
    return out


def _classify_lane(title, description="", buyer="", sector=""):
    """Return lane, product line, evidence, relevance, rationale."""
    text = " " + " ".join(str(x or "") for x in (title, description, sector)).lower() + " "
    b = " " + str(buyer or "").lower() + " "

    excl = [w for w, rx in _EXCL if rx.search(text)]
    core = _hits(_CORE, text)
    partner = _hits(_PARTNER, text)
    signal = _hits(_SIGNAL, text)

    account = next((k for k in KEY_ACCOUNTS if _ACCT[k].search(b)), None)
    dbuyer = next((k for k in DIGITAL_BUYERS if _BUYER[k].search(b)), None)

    # Exclusions win unless a core capability is explicitly present.
    if excl and not core:
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
