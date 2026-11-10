"""Sales workbook: one clean sheet for the sales team, everything else one click away.

Sheets
  Leads      tiers A-C, sales fields only, best first; filter/sort table, tier colours, score
             bars, clickable phone / e-mail / LinkedIn / website, "more contacts" and
             "evidence" links, a Status dropdown and CRM columns to fill in.
  Contacts   every named person at a lead (property managers and owners first), with links.
  Evidence   every fact with its quote and source page; each row links back to its lead.
  Details    all companies incl. tier D and excluded ones (audit trail).
  Guide      how to use the workbook and what every column means.
No formulas; values only.
"""
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.table import Table, TableStyleInfo

INK, GREEN, BLUE, OCHRE, MINT = '1E322D', '3E9A57', '6282D0', 'B08A2A', 'DFF2E1'
HEAD_FILL, HEAD_FONT = PatternFill('solid', fgColor=INK), Font(bold=True, color='FFFFFF')
LINK = Font(color='3F64B5', underline='single')
WRAP = Alignment(wrap_text=True, vertical='top')
TOP = Alignment(vertical='top')
STATUS = ['To call', 'Called - no answer', 'Call back', 'Meeting booked', 'Not interested', 'Customer']

# (source column in the Companies rows, sheet header, width, kind)
LEADS = [
    ('Tier', 'Tier', 6, None), ('Company', 'Company', 32, None),
    ('Apartments', 'Apartments', 22, None), (None, 'Key signal', 30, 'signal'),
    ('Contact person', 'Contact person', 24, None), ('Contact role', 'Role', 20, None),
    (None, 'First contact', 24, 'first'), ('Suggested first step', 'Suggested opener', 60, 'wrap'),
    (None, 'Status', 18, 'status'), (None, 'Called on', 12, 'date'), (None, 'Outcome', 18, None), (None, 'Notes', 30, None),
    ('Segment', 'Segment', 20, None), ('District', 'District', 18, None),
    ('Meters read by hand (upgrade due 2027)', 'Meters read by hand', 14, None), ('Billing today', 'Billing today', 22, None),
    ('Provider change signal', 'Provider change', 18, None), ('Hiring property staff', 'Hiring', 26, None), ('VDIV member', 'VDIV', 7, None),
    ('Fit score', 'Fit', 8, None), ('Access score', 'Reach', 8, None), ('Access', 'Best contact level', 24, None),
    ('Why', 'Why this lead', 46, 'wrap'),
    ('Contact phone', 'Direct phone', 17, 'tel'), ('Contact email', 'Direct e-mail', 26, 'mail'), ('Contact LinkedIn', 'LinkedIn', 11, 'li'),
    ('Office phone', 'Office phone', 17, 'tel'), ('General email', 'General e-mail', 26, 'mail'),
    ('Other relevant contacts', 'Other contacts', 34, 'wrap'), (None, 'All contacts', 12, 'contacts'),
    (None, 'Evidence', 11, 'evidence'), ('Website', 'Website', 24, 'web'), ('Company ID', 'Company ID', 14, None),
]
CONTACT_HEADERS = ['Company', 'Tier', 'Person', 'Role', 'Level', 'Phone', 'E-mail', 'LinkedIn', 'Source page', 'Company ID']
EVIDENCE_HEADERS = ['Company', 'Fact', 'Value', 'Number', 'Person', 'Phone', 'Quote (from the page)', 'Source page', 'Company ID']
GUIDE = [
    ['How to use', 'Start on Leads: filter Tier (A first), sort by Fit or Reach, click a phone, e-mail or LinkedIn to reach out. '
                   '"n people" opens every named contact; "n facts" opens the proof. Track calls in Status, Called on, Outcome and Notes.'],
    ['Sheets', 'Leads = the sales list (tiers A-C). Contacts = every named person at a lead. Evidence = every fact with its quote and page. '
               'Details = all companies, incl. excluded ones. Guide = this page.'],
]


def _header(ws, headers, widths):
    ws.append(headers)
    for i, (h, w) in enumerate(zip(headers, widths), 1):
        c = ws.cell(row=1, column=i)
        c.fill, c.font, c.alignment = HEAD_FILL, HEAD_FONT, Alignment(wrap_text=True, vertical='center')
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 30


def _table(ws, name):
    if ws.max_row < 2: return
    t = Table(displayName=name, ref=f'A1:{get_column_letter(ws.max_column)}{ws.max_row}')
    t.tableStyleInfo = TableStyleInfo(name='TableStyleLight1', showRowStripes=True)
    ws.add_table(t)


def _link(cell, target, text=None, internal=False):
    if text is not None: cell.value = text
    if not target: return
    if internal: cell.hyperlink = Hyperlink(ref=cell.coordinate, location=target)
    else: cell.hyperlink = target
    if text is not None: cell.value = text
    cell.font = LINK


def _phone_uri(p):
    d = ''.join(ch for ch in str(p) if ch.isdigit() or ch == '+')
    return 'tel:' + d if len(d) >= 6 else None


def _host(u):
    return str(u).split('//', 1)[-1].split('/', 1)[0].removeprefix('www.')


def key_signal(r, col):
    """The single most useful buying signal, for a first-glance read of the list."""
    import re
    if r[col['Meters read by hand (upgrade due 2027)']]: return 'Meters read by hand: due 2027'
    if r[col['Provider change signal']]: return 'Provider change: ' + str(r[col['Provider change signal']])
    m = re.search(r'([\d.,]+)\s*units', str(r[col['Apartments']] or ''))
    if m and int(re.sub(r'[.,]', '', m.group(1))) >= 1000: return 'Key account (1,000+ apartments)'
    if r[col['Billing today']]: return 'Billing: ' + str(r[col['Billing today']])
    if r[col['Hiring property staff']]: return 'Hiring property staff'
    if r[col['VDIV member']]: return 'VDIV member: warm intro'
    if r[col['Fit score']] in ('', None): return 'First look: not researched yet'
    return r[col['Segment']] or 'Manages property (type not stated)'


def write_workbook(path, headers, leads, contacts, evidence, detail_headers, details, definitions):
    """headers/leads: the Companies rows (COMPANY_HEADERS order); contacts: rows in CONTACT_HEADERS
    order; evidence: [company_id, company, fact, value, number, person, phone, quote, url]."""
    wb = Workbook(); ws = wb.active; ws.title = 'Leads'
    col = {h: i for i, h in enumerate(headers)}
    lead_ids = [r[col['Company ID']] for r in leads]
    lead_row = {cid: i + 2 for i, cid in enumerate(lead_ids)}

    # Contacts and Evidence first, so Leads can link to their rows.
    wc = wb.create_sheet('Contacts'); _header(wc, CONTACT_HEADERS, [30, 6, 24, 26, 16, 17, 28, 11, 26, 14])
    order = {cid: i for i, cid in enumerate(lead_ids)}
    first_contact, n_contacts = {}, {}
    for r in sorted(contacts, key=lambda r: (order.get(r[-1], 10 ** 9), ['Property manager', 'Owner/director', 'Other staff'].index(r[4]))):
        wc.append(r); i = wc.max_row
        first_contact.setdefault(r[-1], i); n_contacts[r[-1]] = n_contacts.get(r[-1], 0) + 1
        _link(wc.cell(i, 1), f"'Leads'!B{lead_row[r[-1]]}" if r[-1] in lead_row else None, internal=True)
        _link(wc.cell(i, 6), _phone_uri(r[5]) if r[5] else None)
        _link(wc.cell(i, 7), f'mailto:{r[6]}' if r[6] else None)
        _link(wc.cell(i, 8), r[7], 'Profile')
        _link(wc.cell(i, 9), r[8], _host(r[8]) if r[8] else None)
    wc.freeze_panes = 'C2'; _table(wc, 'Contacts')

    we = wb.create_sheet('Evidence'); _header(we, EVIDENCE_HEADERS, [30, 18, 26, 9, 22, 16, 70, 26, 14])
    first_fact, n_facts = {}, {}
    for r in sorted(evidence, key=lambda r: (order.get(r[0], 10 ** 9), r[1], r[2])):
        we.append([r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[0]]); i = we.max_row
        first_fact.setdefault(r[0], i); n_facts[r[0]] = n_facts.get(r[0], 0) + 1
        we.cell(i, 7).alignment = WRAP
        _link(we.cell(i, 1), f"'Leads'!B{lead_row[r[0]]}" if r[0] in lead_row else None, internal=True)
        _link(we.cell(i, 8), r[8], _host(r[8]) if r[8] else None)
    we.freeze_panes = 'B2'; _table(we, 'Evidence')

    # Leads: sales fields only.
    _header(ws, [h for _, h, _, _ in LEADS], [w for _, _, w, _ in LEADS])
    for r in leads:
        cid = r[col['Company ID']]
        ws.append([r[col[src]] if src else '' for src, _, _, _ in LEADS]); i = ws.max_row
        for j, (src, _, _, kind) in enumerate(LEADS, 1):
            c = ws.cell(i, j); v = c.value
            c.alignment = WRAP if kind == 'wrap' else TOP
            if kind == 'tel' and v: _link(c, _phone_uri(v))
            elif kind == 'mail' and v: _link(c, f'mailto:{v}')
            elif kind == 'li' and v: _link(c, v, 'Profile')
            elif kind == 'web' and v: _link(c, v, _host(v))
            elif kind == 'contacts' and n_contacts.get(cid):
                _link(c, f"'Contacts'!A{first_contact[cid]}", f'{n_contacts[cid]} people →', internal=True)
            elif kind == 'evidence' and n_facts.get(cid):
                _link(c, f"'Evidence'!A{first_fact[cid]}", f'{n_facts[cid]} facts →', internal=True)
            elif kind == 'signal': c.value = key_signal(r, col)
            elif kind == 'first':
                ph, em, li = r[col['Contact phone']], r[col['Contact email']], r[col['Contact LinkedIn']]
                oph, oem = r[col['Office phone']], r[col['General email']]
                if ph: _link(c, _phone_uri(ph), f'☎ {ph}')
                elif em: _link(c, f'mailto:{em}', f'✉ {em}')
                elif li: _link(c, li, 'LinkedIn profile')
                elif oph: _link(c, _phone_uri(oph), f'☎ {oph} (office)')
                elif oem: _link(c, f'mailto:{oem}', f'✉ {oem} (office)')
                c.fill = PatternFill('solid', fgColor=MINT); c.font = Font(bold=True, color='1E5C33', underline='single' if c.hyperlink else None)
            elif kind == 'status': c.value = 'To call'
            elif kind == 'date': c.number_format = 'DD.MM.YYYY'
        ws.row_dimensions[i].height = 62
    n = ws.max_row
    letter = {h: get_column_letter(j) for j, (_, h, _, _) in enumerate(LEADS, 1)}
    for tier, colour in ((('A', GREEN), ('B', BLUE), ('C', OCHRE)) if n >= 2 else ()):
        ws.conditional_formatting.add(f"{letter['Tier']}2:{letter['Tier']}{n}", CellIsRule(operator='equal', formula=[f'"{tier}"'],
                                      fill=PatternFill(fill_type='solid', bgColor=colour, fgColor=colour), font=Font(bold=True, color='FFFFFF')))
    dv = DataValidation(type='list', formula1='"' + ','.join(STATUS) + '"', allow_blank=True)
    ws.add_data_validation(dv); dv.add(f"{letter['Status']}2:{letter['Status']}{max(n, 2)}")
    ws.freeze_panes = 'C2'; _table(ws, 'Leads')

    wd = wb.create_sheet('Details'); _header(wd, detail_headers, [14 if h == 'Company ID' else 30 if h == 'Company' else 18 for h in detail_headers])
    for r in details: wd.append(r)
    wd.freeze_panes = 'C2'; _table(wd, 'Details')

    wg = wb.create_sheet('Guide'); _header(wg, ['Item', 'What it means'], [24, 110])
    for r in GUIDE + definitions:
        wg.append(r); wg.cell(wg.max_row, 2).alignment = WRAP; wg.cell(wg.max_row, 1).font = Font(bold=True)
    wb.active = 0
    wb.save(path)
    return dict(leads=len(leads), contacts=wc.max_row - 1, evidence=we.max_row - 1, details=len(details))
