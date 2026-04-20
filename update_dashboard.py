#!/usr/bin/env python3
"""
Script de mise à jour du dashboard Le Club Paul Barbosa.
Récupère les données depuis l'API Circle + Tally et redéploie sur Surge.sh.

OPTIMISATION API CIRCLE:
- Cache local des participants par événement (ne re-fetch jamais un event passé)
- Seuls les events futurs + events récents non cachés sont fetchés
- ~30 appels/exécution au lieu de ~470
- ~120 appels/mois au lieu de ~1900
"""

import json
import hashlib
import subprocess
import sys
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

# === CONFIG ===
CIRCLE_API_TOKEN = "pUU1QpNS8YD26R1NnaF8HBoAdt7tZAZs"
CIRCLE_BASE_URL = "https://leclub-paulbarbosa.circle.so/api/admin/v2"
TALLY_API_TOKEN = "tly-GjDLAzHKf3hrlSuoYSN1vBVZerCKS6eU"
TALLY_FORM_ID = "3Ee6xB"
TALLY_PHONE_QUESTION_ID = "vDBDQd"
TALLY_EMAIL_QUESTION_ID = "Bp1pPA"
TALLY_NAME_QUESTION_ID = "beke02"
TALLY_BIRTHDAY_QUESTION_ID = "D7xEM5"
DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
TALLY_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tally_cache.json")

# Membres à exclure (IDs Circle)
EXCLUDED_MEMBER_IDS = {
    79170903,  # Clara Ventrella (influxacademy)
    79097733,  # Nicolino Emmanuelle
    78273597,  # Yanne Martial Dayoro
    78255109,  # ANCIEN COMPTE - Yanne Martial Dayoro
    76846781,  # Mateo Hamdine (influxacademy)
    76622262,  # Stéphane Beignet
    73017957,  # Emma Perret
    71021137,  # Ariel Ephraimi (influxacademy)
    66549324,  # Valère Corréard (influxacademy)
    52137707,  # Emma Khoury
    44105752,  # Emma du Club
    31600602,  # Quentin Barrère
    19845459,  # Paul Barbosa
}
EXCLUDED_EMAIL_PATTERNS = ["influxacademy.com", "influxcrew.com"]

api_call_count = 0


def curl_get(url, auth_header):
    """HTTP GET via curl (Cloudflare blocks urllib)."""
    global api_call_count
    api_call_count += 1
    result = subprocess.run(
        ["curl", "-s", "-H", auth_header, url],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def circle_api(endpoint, params=None):
    """Call Circle API."""
    url = f"{CIRCLE_BASE_URL}/{endpoint}"
    if params:
        url += "?" + urlencode(params)
    return curl_get(url, f"Authorization: Token {CIRCLE_API_TOKEN}")


def circle_api_all(endpoint, params=None, max_pages=50):
    """Fetch all pages from Circle API."""
    all_records = []
    page = 1
    base_params = dict(params or {})
    base_params["per_page"] = 100
    while page <= max_pages:
        base_params["page"] = page
        data = circle_api(endpoint, base_params)
        records = data.get("records", [])
        all_records.extend(records)
        if not data.get("has_next_page", False):
            break
        page += 1
    return all_records


def tally_api(endpoint):
    """Call Tally API (does NOT count toward Circle quota)."""
    return curl_get(
        f"https://api.tally.so/{endpoint}",
        f"Authorization: Bearer {TALLY_API_TOKEN}"
    )


def is_excluded(member):
    """Check if a member should be excluded."""
    if member["id"] in EXCLUDED_MEMBER_IDS:
        return True
    email = member.get("email", "").lower()
    for pattern in EXCLUDED_EMAIL_PATTERNS:
        if pattern in email:
            return True
    return False


def load_cache():
    """Load cached event attendees data."""
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"event_attendees": {}, "last_updated": None}


def save_cache(cache):
    """Save cache to disk."""
    cache["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False)


def load_tally_cache():
    """Load cached Tally phone data."""
    if os.path.exists(TALLY_CACHE_PATH):
        with open(TALLY_CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"phone_map": {}, "last_submission_date": None}


def save_tally_cache(data):
    """Save Tally cache."""
    with open(TALLY_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def get_all_members():
    """Get all community members (excluding filtered ones). ~2 API calls."""
    print("📥 Récupération des membres...")
    all_members = circle_api_all("community_members")
    members = [m for m in all_members if not is_excluded(m)]
    excluded = len(all_members) - len(members)
    print(f"   → {len(all_members)} membres total, {excluded} exclus, {len(members)} retenus")
    return members


def get_all_events():
    """Get all events. ~5 API calls."""
    print("📥 Récupération des événements...")
    events = circle_api_all("events")
    print(f"   → {len(events)} événements récupérés")
    return events


def get_event_attendees(event_id):
    """Get attendees for a specific event. 1+ API calls."""
    try:
        all_attendees = []
        page = 1
        while True:
            data = circle_api("event_attendees", {
                "event_id": event_id, "per_page": 100, "page": page
            })
            records = data.get("records", [])
            all_attendees.extend(records)
            if not data.get("has_next_page", False) or len(records) == 0:
                break
            page += 1
        return all_attendees
    except Exception:
        return []


def normalize_name(n):
    """
    Normalize a name for strict matching.
    Returns a tuple of sorted lowercase ASCII tokens (no accents).
    "Marie-Lorina LAMOLY" → ('lamoly', 'lorina', 'marie')
    "Lamoly Marie Lorina" → ('lamoly', 'lorina', 'marie')
    Only names with 2+ tokens are considered valid (to avoid single-name false matches).
    """
    import unicodedata
    if not n:
        return None
    # Remove accents
    normalized = unicodedata.normalize('NFD', n).encode('ascii', 'ignore').decode('ascii')
    # Extract lowercase alphabetic tokens
    tokens = re.findall(r'[a-z]+', normalized.lower())
    # Require at least 2 tokens (first name + last name) for safe matching
    if len(tokens) < 2:
        return None
    return tuple(sorted(tokens))


def get_tally_data():
    """
    Get Tally form submissions (phone, birthday, name, email).
    Uses cache: only fetches NEW submissions since last run.
    Does NOT count toward Circle API quota.

    Returns dict with keys:
      - by_email: {email -> {phone, birthday, name}}
      - by_name:  {normalized_name -> {phone, birthday, name, email}}
    """
    print("📥 Récupération des données Tally...")
    tally_cache = load_tally_cache()

    # Migrate old format if needed
    by_email = tally_cache.get("by_email", {})
    by_name = tally_cache.get("by_name", {})
    if not by_email and "phone_map" in tally_cache:
        for email, phone in tally_cache["phone_map"].items():
            by_email[email] = {"phone": phone, "birthday": None, "name": ""}

    last_date = tally_cache.get("last_submission_date")

    if last_date:
        print(f"   → {len(by_email)} enregistrements en cache (depuis {last_date[:10]})")

    page = 1
    new_count = 0
    newest_date = last_date

    while True:
        data = tally_api(f"forms/{TALLY_FORM_ID}/submissions?limit=100&page={page}")
        submissions = data.get("submissions", [])
        if not submissions:
            break

        stop = False
        for sub in submissions:
            if not sub.get("isCompleted"):
                continue

            sub_date = sub.get("submittedAt", "")

            if last_date and sub_date <= last_date:
                stop = True
                break

            email = None
            phone = None
            birthday = None
            name = None
            for resp in sub.get("responses", []):
                qid = resp.get("questionId")
                if qid == TALLY_EMAIL_QUESTION_ID:
                    email = resp.get("answer", "")
                elif qid == TALLY_PHONE_QUESTION_ID:
                    phone = resp.get("answer", "")
                elif qid == TALLY_BIRTHDAY_QUESTION_ID:
                    birthday = resp.get("answer", "")
                elif qid == TALLY_NAME_QUESTION_ID:
                    name = resp.get("answer", "")

            record = {
                "phone": (phone or "").strip(),
                "birthday": (birthday or "").strip() or None,
                "name": (name or "").strip()
            }

            if email:
                email_key = email.lower().strip()
                if email_key not in by_email:
                    by_email[email_key] = record
                    new_count += 1
                else:
                    # Keep existing but merge missing fields
                    existing = by_email[email_key]
                    if not existing.get("phone") and record["phone"]:
                        existing["phone"] = record["phone"]
                    if not existing.get("birthday") and record["birthday"]:
                        existing["birthday"] = record["birthday"]

            if name:
                name_key = normalize_name(name)
                if name_key:
                    # Store with serialized tuple key (JSON-safe)
                    key_str = "|".join(name_key)
                    if key_str not in by_name:
                        by_name[key_str] = {**record, "email": email}
                    else:
                        # Merge missing fields
                        existing = by_name[key_str]
                        if not existing.get("phone") and record["phone"]:
                            existing["phone"] = record["phone"]
                        if not existing.get("birthday") and record["birthday"]:
                            existing["birthday"] = record["birthday"]

            if newest_date is None or sub_date > newest_date:
                newest_date = sub_date

        if stop or not data.get("hasMore", False):
            break
        page += 1

    if new_count > 0:
        print(f"   → {new_count} nouveaux enregistrements ajoutés")

    tally_cache = {
        "by_email": by_email,
        "by_name": by_name,
        "last_submission_date": newest_date
    }
    save_tally_cache(tally_cache)

    print(f"   → {len(by_email)} enregistrements Tally au total")
    return tally_cache


def build_dashboard_data(members, events):
    """
    Build the EMBEDDED_DATA structure for the dashboard.

    OPTIMISATION: utilise un cache local pour les participants par événement.
    - Les événements passés déjà en cache ne sont JAMAIS re-fetchés
    - Seuls les événements futurs + nouveaux événements passés sont fetchés
    - Économise ~400+ appels API par exécution
    """
    now = datetime.now(timezone.utc)
    cache = load_cache()
    cached_attendees = cache.get("event_attendees", {})

    # Get Tally data (phones + birthdays)
    tally_data = get_tally_data()
    tally_by_email = tally_data.get("by_email", {})
    tally_by_name = tally_data.get("by_name", {})
    # Legacy phone_map for backward compat (just emails -> phones)
    phone_map = {k: v.get("phone", "") for k, v in tally_by_email.items() if v.get("phone")}

    # Separate future and past events
    future_events = []
    past_events = []

    for event in events:
        starts_at = event.get("starts_at", "")
        if not starts_at:
            continue
        event_date = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))

        # Skip events older than 2 years
        if (now - event_date).days > 730:
            continue

        if event_date > now:
            future_events.append(event)
        else:
            past_events.append(event)

    # === FUTURE EVENTS: always fetch fresh (for RSVP tracking) ===
    print(f"📥 Récupération des RSVPs pour {len(future_events)} événements futurs...")
    future_rsvp_member_ids = set()
    api_calls_future = 0

    for event in future_events:
        attendees = get_event_attendees(event["id"])
        api_calls_future += 1
        for att in attendees:
            mid = att.get("community_member_id")
            if mid:
                future_rsvp_member_ids.add(mid)

    print(f"   → {api_calls_future} appels API, {len(future_rsvp_member_ids)} membres inscrits")

    # === PAST EVENTS: use cache, only fetch uncached ones ===
    past_events_sorted = sorted(past_events, key=lambda e: e.get("starts_at", ""), reverse=True)
    member_participations = {}  # member_id -> list of events

    uncached_count = 0
    cached_count = 0

    for event in past_events_sorted:
        eid = str(event["id"])
        starts_at = event.get("starts_at", "")

        if eid in cached_attendees:
            # Use cached data — NO API call
            attendees_data = cached_attendees[eid]
            cached_count += 1
        else:
            # Fetch from API and cache
            attendees = get_event_attendees(event["id"])
            attendees_data = [
                {"community_member_id": a.get("community_member_id")}
                for a in attendees
            ]
            cached_attendees[eid] = attendees_data
            uncached_count += 1

            if uncached_count % 20 == 0:
                print(f"   → {uncached_count} nouveaux événements fetchés...")

        # Build participation map
        for att in attendees_data:
            mid = att.get("community_member_id")
            if mid:
                if mid not in member_participations:
                    member_participations[mid] = []
                member_participations[mid].append({
                    "event_name": event["name"],
                    "event_date": starts_at
                })

    print(f"📊 Événements passés: {cached_count} en cache, {uncached_count} nouveaux fetchés")

    # Save updated cache
    cache["event_attendees"] = cached_attendees
    save_cache(cache)

    # Build member data
    nouveaux_sans_event = []
    membres_inactifs_30j = []
    membres_fideles_6mois = []
    tous_les_membres = []
    active_count = 0

    for member in members:
        if not member.get("active", True):
            continue

        mid = member["id"]
        name = member.get("name", "")
        email = member.get("email", "")
        created_at = member.get("created_at", "")
        last_seen_at = member.get("last_seen_at", "")

        # Phone: Tally by email → Tally by name → Circle profile
        phone = ""
        email_key = email.lower().strip()
        if email_key and email_key in tally_by_email:
            phone = tally_by_email[email_key].get("phone", "") or ""

        if not phone:
            name_tokens = normalize_name(name)
            if name_tokens:
                key_str = "|".join(name_tokens)
                if key_str in tally_by_name:
                    phone = tally_by_name[key_str].get("phone", "") or ""

        if not phone:
            for pf in member.get("profile_fields", []):
                if "phone" in pf.get("key", "").lower():
                    cmpf = pf.get("community_member_profile_field", {})
                    if cmpf:
                        phone = cmpf.get("text", "") or ""
                    break

        # Days since joined
        if created_at:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            days_since_joined = (now - created).days
        else:
            days_since_joined = 0

        # Past participations
        participations = member_participations.get(mid, [])
        total_participations = len(participations)

        # Last event date (past events only)
        last_event_date = None
        days_since_last_event = None
        if participations:
            participations_sorted = sorted(
                participations, key=lambda p: p["event_date"], reverse=True
            )
            last_event_date = participations_sorted[0]["event_date"]
            led = datetime.fromisoformat(last_event_date.replace("Z", "+00:00"))
            days_since_last_event = (now - led).days

        has_future_rsvp = mid in future_rsvp_member_ids

        member_data = {
            "id": mid,
            "name": name,
            "email": email,
            "phone": phone,
            "created_at": created_at,
            "days_since_joined": days_since_joined,
            "last_seen_at": last_seen_at,
            "total_participations": total_participations,
            "last_event_date": last_event_date,
            "days_since_last_event": days_since_last_event,
            "participations": participations[:10]
        }

        tous_les_membres.append(member_data)

        # === ONGLET 1: Nouveaux sans événement ===
        if total_participations == 0 and not has_future_rsvp:
            nouveaux_sans_event.append(member_data)

        # === ONGLET 2: Inactifs +30j ===
        if total_participations > 0:
            if days_since_last_event is not None and days_since_last_event > 30:
                if not has_future_rsvp:
                    membres_inactifs_30j.append(member_data)

        # === ONGLET 3: Fidèles +6 mois ===
        if days_since_joined >= 180 and total_participations >= 3:
            membres_fideles_6mois.append(member_data)

        if total_participations > 0:
            active_count += 1

    total = len(tous_les_membres)

    # === ONGLET 5: Anniversaires ===
    from datetime import date
    today = date.today()
    anniversaires = []
    matched_by_email = 0
    matched_by_name = 0

    for m in tous_les_membres:
        email_key = (m.get("email") or "").lower().strip()
        birthday = None

        if email_key and email_key in tally_by_email:
            birthday = tally_by_email[email_key].get("birthday")
            if birthday:
                matched_by_email += 1

        if not birthday:
            name_tokens = normalize_name(m.get("name", ""))
            if name_tokens:
                key_str = "|".join(name_tokens)
                if key_str in tally_by_name:
                    birthday = tally_by_name[key_str].get("birthday")
                    if birthday:
                        matched_by_name += 1

        if not birthday:
            continue

        try:
            bday = datetime.strptime(birthday, "%Y-%m-%d").date()
            this_year = bday.replace(year=today.year)
            if this_year < today:
                next_bday = bday.replace(year=today.year + 1)
            else:
                next_bday = this_year
            days_until = (next_bday - today).days
            age_turning = next_bday.year - bday.year

            anniversaires.append({
                "id": m["id"],
                "name": m["name"],
                "email": m["email"],
                "phone": m.get("phone", ""),
                "birthday": birthday,
                "birthday_display": bday.strftime("%d/%m/%Y"),
                "birthday_short": bday.strftime("%d/%m"),
                "days_until_birthday": days_until,
                "age_turning": age_turning,
                "birth_year": bday.year
            })
        except ValueError:
            pass

    anniversaires.sort(key=lambda x: x["days_until_birthday"])

    # === ONGLET 6: Créateurs de créneaux (0 appel API supplémentaire) ===
    work_patterns = [
        'cowork', 'co-work', 'journée', 'aprem', 'après-midi', 'après midi',
        'matinée', 'matin ', 'session travail', 'work en remote', 'starting block',
        'focus', 'pomodoro', 'felicità', 'felicita', 'comptoir avalon',
        'prêt à manger', 'voie 15', 'digital village', 'koneko', 'wojo',
        'drawing hotel', 'mob house', 'café studio', 'café qj', 'kafeibaie',
        'kāfēibaie', 'tribe hotel', 'shack', 'miliki', 'extraction coffee',
        'nelson', 'ateliers gaïté', 'cosy corner', 'pavillon des canaux',
        'montgolfière', 'malt', 'climbing district', 'blédards', 'ho/ba',
        'péniche annette', 'marriott', 'hug ', 'bingsu', 'cnd', 'café du club',
        'ground control',
    ]
    non_work_patterns = [
        'masterclass', 'soirée rencontres', 'soirée -', 'scène ouverte',
        'table ronde', 'atelier business', 'appel de bienvenue',
        'dîner', 'run ', '10 000 pas', '6k run', 'danse', 'salsa',
        'pitch', 'shooting photo', 'bowling', 'expo', 'keynote',
        'pilote ton mois', 'food tour',
    ]
    members_emails = {m["email"].lower().strip() for m in tous_les_membres}

    creneaux_creators = {}
    for e in events:
        ename = e.get("name", "")
        el = ename.lower()
        if any(p in el for p in non_work_patterns):
            continue
        if not any(p in el for p in work_patterns):
            continue
        cid = e.get("community_member_id")
        if cid in EXCLUDED_MEMBER_IDS:
            continue
        if cid not in creneaux_creators:
            creneaux_creators[cid] = {
                "name": e.get("member_name", ""),
                "email": e.get("member_email", ""),
                "phone": "",
                "total_creneaux": 0,
                "last_date": "",
                "last_event": "",
            }
        creneaux_creators[cid]["total_creneaux"] += 1
        if e.get("starts_at", "") > creneaux_creators[cid]["last_date"]:
            creneaux_creators[cid]["last_date"] = e.get("starts_at", "")
            creneaux_creators[cid]["last_event"] = ename

    # Add phones + calculate days + filter
    today_date = now.date()
    creneaux_list = []
    for c in creneaux_creators.values():
        # Phone from Tally
        ek = (c["email"] or "").lower().strip()
        if ek in phone_map:
            c["phone"] = phone_map[ek]
        else:
            nt = normalize_name(c["name"])
            if nt:
                ks = "|".join(nt)
                if ks in tally_by_name:
                    c["phone"] = tally_by_name[ks].get("phone", "")

        # Days since last
        if c["last_date"]:
            ld = datetime.fromisoformat(c["last_date"].replace("Z", "+00:00")).date()
            c["days_since_last"] = (today_date - ld).days
        else:
            c["days_since_last"] = 9999

        # Filter: < 100 days AND still a member
        if c["days_since_last"] > 100:
            continue
        if ek not in members_emails:
            continue
        creneaux_list.append(c)

    creneaux_list.sort(key=lambda x: x["last_date"], reverse=True)

    data = {
        "nouveaux_sans_event": nouveaux_sans_event,
        "membres_inactifs_30j": membres_inactifs_30j,
        "membres_fideles_6mois": membres_fideles_6mois,
        "tous_les_membres": tous_les_membres,
        "anniversaires": anniversaires,
        "creneaux_creators": creneaux_list,
        "stats": {
            "total_membres": total,
            "membres_actifs": active_count,
            "membres_sans_participation": len(nouveaux_sans_event),
            "membres_inactifs_30j": len(membres_inactifs_30j),
            "membres_fideles_6mois": len(membres_fideles_6mois)
        }
    }

    print(f"\n📊 Résumé:")
    print(f"   Total membres: {total}")
    print(f"   Membres actifs: {active_count}")
    print(f"   Nouveaux sans event: {len(nouveaux_sans_event)}")
    print(f"   Inactifs +30j: {len(membres_inactifs_30j)}")
    print(f"   Fidèles +6 mois: {len(membres_fideles_6mois)}")
    print(f"   Anniversaires: {len(anniversaires)} (par email: {matched_by_email}, par nom: {matched_by_name})")
    print(f"   Créateurs de créneaux: {len(creneaux_list)}")

    return data


def update_html(data):
    """Update EMBEDDED_DATA in the HTML file."""
    print("\n📝 Mise à jour du fichier HTML...")

    with open(DASHBOARD_PATH, 'r', encoding='utf-8') as f:
        html = f.read()

    data_json = json.dumps(data, indent=2, ensure_ascii=False)

    match = re.search(r'const EMBEDDED_DATA = \{.*?\n\};', html, re.DOTALL)
    if not match:
        print("   ⚠️ EMBEDDED_DATA non trouvé")
        return False

    new_html = html[:match.start()] + f'const EMBEDDED_DATA = {data_json};' + html[match.end():]

    if new_html == html:
        print("   ⚠️ Aucune modification détectée dans le HTML")
        return False

    with open(DASHBOARD_PATH, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print(f"   ✅ Fichier mis à jour ({len(new_html)} chars)")
    return True


def deploy_to_github_pages():
    """Deploy the updated HTML to GitHub Pages via git push."""
    print("\n🚀 Déploiement sur GitHub Pages...")

    deploy_dir = os.path.dirname(DASHBOARD_PATH)
    result = subprocess.run(
        ["git", "-C", deploy_dir, "add", "index.html"],
        capture_output=True, text=True
    )
    result = subprocess.run(
        ["git", "-C", deploy_dir, "diff", "--cached", "--quiet"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("   ℹ️ Aucun changement à commiter")
        return

    result = subprocess.run(
        ["git", "-C", deploy_dir, "commit", "-m",
         f"Update dashboard data {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        capture_output=True, text=True
    )
    print(f"   Commit: {result.stdout.strip()}")

    result = subprocess.run(
        ["git", "-C", deploy_dir, "push"],
        capture_output=True, text=True, timeout=60
    )

    if result.returncode == 0:
        print("   ✅ Déploiement réussi !")
    else:
        print(f"   ⚠️ Erreur push: {result.stderr[:200]}")

    print(f"   🔗 https://paulbarbosa-club.github.io/leclub-dashboard/")


def main():
    global api_call_count
    api_call_count = 0

    print(f"{'='*50}")
    print(f"🔄 Mise à jour du Dashboard Le Club")
    print(f"   {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*50}\n")

    try:
        members = get_all_members()
        events = get_all_events()
        data = build_dashboard_data(members, events)

        print(f"\n📡 Appels API Circle utilisés: {api_call_count}")

        if update_html(data):
            deploy_to_github_pages()
        else:
            print("\n⚠️ Pas de changement, déploiement annulé")

        print(f"\n{'='*50}")
        print("✅ Mise à jour terminée avec succès !")
        print(f"{'='*50}")

    except Exception as e:
        print(f"\n❌ Erreur: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
