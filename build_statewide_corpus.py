import os
import random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# Master administrative hierarchy: 7 Divisions -> 41 Districts
RAJASTHAN_ADMIN_HIERARCHY = {
    "Jaipur Division": [
        "Jaipur", "Alwar", "Dausa", "Jhunjhunu", "Sikar", 
        "Khairthal-Tijara", "Kotputli-Behror"
    ],
    "Jodhpur Division": [
        "Jodhpur", "Barmer", "Jaisalmer", "Jalore", "Pali", 
        "Sirohi", "Balotra", "Phalodi"
    ],
    "Ajmer Division": [
        "Ajmer", "Bhilwara", "Nagaur", "Tonk", 
        "Beawar", "Didwana-Kuchaman"
    ],
    "Udaipur Division": [
        "Udaipur", "Banswara", "Chittorgarh", "Dungarpur", 
        "Pratapgarh", "Rajsamand", "Salumbar"
    ],
    "Bikaner Division": [
        "Bikaner", "Churu", "Hanumangarh", "Sri Ganganagar"
    ],
    "Kota Division": [
        "Kota", "Baran", "Bundi", "Jhalawar"
    ],
    "Bharatpur Division": [
        "Bharatpur", "Dholpur", "Karauli", "Sawai Madhopur", "Deeg"
    ]
}

DEPARTMENTS = [
    ("PWD", "Public Works Department"),
    ("PHED", "Public Health Engineering Department"),
    ("WRD", "Water Resources Department"),
    ("DLB", "Directorate of Local Bodies"),
    ("MEDIC", "Department of Medical Education"),
    ("FORES", "Forest Department"),
    ("UIT", "Urban Improvement Trust")
]

os.makedirs("data/districts", exist_ok=True)
random.seed(42)
np.random.seed(42)

total_notices = 0

for division_name, districts in RAJASTHAN_ADMIN_HIERARCHY.items():
    for district in districts:
        # Generate 400 to 500 notices per district
        n_records = random.randint(420, 480)
        total_notices += n_records
        
        records = []
        for i in range(n_records):
            dept_code, dept_name = random.choice(DEPARTMENTS)
            
            # Use real local authority naming for development trusts
            if dept_code == "UIT":
                authority = f"UIT {district}" if district in ["Bikaner", "Udaipur", "Kota", "Bharatpur", "Alwar"] else f"Municipal Council {district}"
            else:
                authority = f"{dept_name} ({district} Circle)"
                
            tender_id = f"2026_{dept_code}_{district[:4].upper()}_{random.randint(100000, 999999)}"
            nit_no = f"NIT_{random.randint(10, 99)}/{district[:3].upper()}/2025-26"
            
            # Value tiers: Minor works (<10L), Medium (10L-50L), Major (50L-2Cr), High-Value (>2Cr)
            tier_prob = np.random.choice([0, 1, 2, 3], p=[0.55, 0.25, 0.14, 0.06])
            if tier_prob == 0:
                cost_inr = round(random.uniform(150000, 950000), 2)
                statutory_days = 7
            elif tier_prob == 1:
                cost_inr = round(random.uniform(1050000, 4800000), 2)
                statutory_days = 10
            elif tier_prob == 2:
                cost_inr = round(random.uniform(5200000, 19000000), 2)
                statutory_days = 15
            else:
                cost_inr = round(random.uniform(21000000, 120000000), 2)
                statutory_days = 30

            # Realistic violation rate (~3.2% non-compliance, concentrated in high-value works)
            is_anomaly = False
            if statutory_days >= 15 and random.random() < 0.12:
                is_anomaly = True
                actual_days = random.randint(3, statutory_days - 3)
            elif random.random() < 0.015:
                is_anomaly = True
                actual_days = max(2, statutory_days - random.randint(2, 5))
            else:
                actual_days = statutory_days + random.randint(0, 8)

            # Assign realistic tender publication dates across the fiscal year
            base_date = datetime(2025, 4, 1)
            pub_date = base_date + timedelta(days=random.randint(0, 360))
            
            records.append({
                "tender_id": tender_id,
                "nit_no": nit_no,
                "division": authority,
                "district": district,
                "administrative_division": division_name,
                "tender_amount": cost_inr,
                "estimated_cost_lac": round(cost_inr / 100000.0, 2),
                "published_duration_days": int(actual_days),
                "publication_date": pub_date.strftime("%Y-%m-%d")
            })
            
        csv_path = f"data/districts/{district.lower().replace(' ', '_').replace('-', '_')}.csv"
        pd.DataFrame(records).to_csv(csv_path, index=False)

print(f"✅ Generated 41 district files across 7 divisions with {total_notices:,} total tenders in data/districts/")
