existing_ids = set(df_master['tender_id'].tolist())

# As it scrapes new rows:
if tender_id not in existing_ids:
    new_records.append(new_row)
