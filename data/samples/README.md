# Sample CSV files (fictional)

Import these from **Inquiry & Booking Management → Import / Export**. Import inquiries first, then quotes.

- `sample_inquiries.csv` – 3 valid rows, 3 intentionally invalid rows (zero passengers, unsupported currency,
  return before departure) and 1 intentional duplicate. The preview shows each row's status and reason before anything is saved.
- `sample_quotes.csv` – quotes linked by `inquiry_customer_label` (or `inquiry_id`); 3 valid rows and 2 intentionally invalid rows.

List columns (`requested_services`, `included_services`, `excluded_services`) use `;` as the separator.
Allowed quote service items: accommodation, ground_transport, catering, pet_travel, special_luggage, experiences,
wifi, landing_and_handling_fees, taxes, de_icing, crew_overnight, positioning.

Rows are never overwritten unless they name an existing `inquiry_id` / `quote_id` **and** you tick
"Overwrite existing records". All rows are committed in one transaction.
