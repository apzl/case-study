"""Shared field guide for extraction (only fields that change the sales conversation).
Code checks in core.check_claim enforce the literal rules."""

SCOPE_RULES = '''
What each field means (record only what the quote states; unknown = no claim):
- berlin_office / berlin_service / berlin_property = "yes" when the quote shows the
  company's own Berlin address, Berlin in its service area, or a building it manages in
  Berlin. Companies based outside Berlin are fine; one Berlin fact is enough.
- management_type answers one sales question: does this company look after residential
  buildings? weg (WEG-Verwaltung, Eigentümergemeinschaften); whole_building_rental (it
  manages rented homes for owners: Mietverwaltung, Miethausverwaltung, Hausverwaltung
  für Wohnimmobilien/Wohnhäuser, tenant service such as Mietersprechstunde or preparing
  tenants' Betriebs-/Heizkostenabrechnung); owned_stock (it owns and rents out housing,
  e.g. a Genossenschaft). Generic wording that clearly concerns homes is enough; do not
  demand the exact word "Miethaus". Single-flat (SEV) only and commercial-only: no claim.
  Facts about a sister or parent company are not about this company.
- units: a published count of apartments the company manages or owns. quantity:
  residential_units (Wohnungen/Wohneinheiten), mixed_units (Wohn- und Gewerbeeinheiten),
  unspecified_units (no type). qualifier: exact, approximately (ca.), at_least (über,
  mehr als). geography: berlin only for Berlin city; Berlin und Brandenburg / Großraum
  Berlin = berlin_region. Report each published number separately, never add them up.
  Parking spaces, single reference projects and group totals are not company totals.
- metering: nonradio_consumption (meters read on site, not radio), radio_consumption
  (Funk meters), external_provider (an outside Messdienst reads or bills),
  internal_provider (the company prepares the Heizkostenabrechnung itself). Smoke alarms
  are not consumption meters. General articles about the law are not facts about them.
  nonradio_consumption needs wording that meters are read on site (Ablesung vor Ort,
  Ableser, Zutritt) or are not remote-readable; a sentence about billing alone is not it.
  A provider named only in a privacy notice or as an example in a list is not their
  provider.
- metering_provider / software: the name exactly as written (Techem, ista, Brunata,
  Minol, Kalo; Domus, Haufe PowerHaus, Karthago, immoware24, Wodis, casavi, facilioo).
  A name visible only in a link (e.g. a tenant portal at x.mycasavi.com) counts.
- hkvo_2027 (meters must be remote-readable by 2027): manual_reading (Ableser,
  Ablesetermin, Selbstablesung), retrofit_planned (switch to remote reading planned),
  remote_reading_installed (Funk/fernablesbar already installed).
- metering_contract: end_date_known (Messdienst contract end or Laufzeit stated),
  switch_intent (decided or announced change of Messdienst), tender (Ausschreibung).
- contact: person, role, email and phone only when printed together in the quote; the
  person's name must be in the quote. Prefer people who look after properties
  (Objektbetreuer, Verwalter) and owners/directors, with their direct phone. A general
  company line or inbox has person and role null. Never guess an email or phone. Set
  location to the office city named with the contact, else null.
'''

COMPLETENESS_RULES = '''
Before returning, check each family (Berlin, management, scale, metering, contact) and list
in researched_families only those whose relevant pages you actually inspected. Long blocks
come as excerpts with start/end offsets: cite the original block_id and quote inside one
excerpt.
'''
