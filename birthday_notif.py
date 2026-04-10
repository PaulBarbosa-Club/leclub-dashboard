#!/usr/bin/env python3
"""
Vérifie si un membre du Club fête son anniversaire aujourd'hui
et envoie une notification sur Google Chat.
Fonctionne sans appel API — lit les données depuis index.html.
"""

import json
import re
import sys
from datetime import datetime, date
from urllib.request import Request, urlopen

GOOGLE_CHAT_WEBHOOK = (
    "https://chat.googleapis.com/v1/spaces/AAQAJGFnAao/messages"
    "?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI"
    "&token=Ae9JZ4Iq91qSzR7ksIECi2zNgzI0TnIFokL69oscr6k"
)


def main():
    today = date.today()
    print(f"🎂 Vérification des anniversaires — {today.strftime('%d/%m/%Y')}")

    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    match = re.search(r"const EMBEDDED_DATA = (\{.*?\n\});", html, re.DOTALL)
    if not match:
        print("❌ EMBEDDED_DATA non trouvé")
        sys.exit(1)

    data = json.loads(match.group(1))
    anniversaires = data.get("anniversaires", [])

    # Find members whose birthday is TODAY (based on birthday field, not days_until)
    birthday_today = []
    for m in anniversaires:
        try:
            bday = datetime.strptime(m["birthday"], "%Y-%m-%d").date()
            if bday.month == today.month and bday.day == today.day:
                age = today.year - bday.year
                birthday_today.append({
                    "name": m["name"],
                    "age": age,
                    "phone": m.get("phone", ""),
                    "email": m.get("email", "")
                })
        except (ValueError, KeyError):
            continue

    if not birthday_today:
        print("ℹ️ Aucun anniversaire aujourd'hui")
        return

    # Build Google Chat message
    for member in birthday_today:
        phone = member["phone"] or "non renseigné"
        text = (
            f"🎂 *Anniversaire aujourd'hui !*\n\n"
            f"*{member['name']}* fête ses *{member['age']} ans* aujourd'hui !\n"
            f"📞 {phone}\n"
            f"📧 {member['email']}"
        )

        print(f"   → {member['name']} ({member['age']} ans)")

        payload = json.dumps({"text": text}).encode("utf-8")
        req = Request(
            GOOGLE_CHAT_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST"
        )

        try:
            with urlopen(req) as resp:
                if resp.status == 200:
                    print("   ✅ Notification envoyée sur Google Chat")
                else:
                    print(f"   ⚠️ Réponse: {resp.status}")
        except Exception as e:
            print(f"   ❌ Erreur: {e}")

    print(f"\n🎉 {len(birthday_today)} notification(s) envoyée(s)")


if __name__ == "__main__":
    main()
