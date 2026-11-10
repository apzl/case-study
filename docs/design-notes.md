# How the pipeline came together

What was tried, what worked, what didn't, and how each lesson shaped the next step.

## 1. Finding the companies

**Started with directories.** The VDIV member directory looked like the natural list of Berlin property
managers. It gave a few hundred firms with official names, but almost no facts about them.

**Switched to maps first.** Google Places (14 search terms across 12 districts) and OpenStreetMap gave
the broad picture: 1,561 + 76 offices, each with an address, office phone and map position. The
directories stayed, because they add something the map can't: VDIV membership is a warm introduction
(Heidi is VDIV's partner), and BBU, WBGD, BVI and the state-owned list bring in the large housing
companies and cooperatives. 2,093 companies in total.

**Duplicates came from everywhere.** The same firm appeared under its map listing and its directory
name. Matching on website domain, phone number, or name plus postcode merged 84 of them.

## 2. Researching each company

**The company's own website is the best source.** Its legal notice, team, contact and service pages
answer most questions: what they manage, how many apartments, who runs the company, sometimes how
heating costs are billed. A crawler saves every page, so every fact can be traced back later.

**Firms without a website were the hard part.** A web search finds their own site or directory pages
(Gelbe Seiten, 11880, listing portals), but it is the most expensive step per useful result: many
small firms are simply not online. Merging the searches into one call halved the cost.

**Not every signal is published.** Apartment counts appear for about a fifth of companies; the
metering provider, contract dates and the 2027 meter status for only 1–2%. So the scoring leans on
what companies do say (what they manage, how big they are) and treats the rare signals as strong
extras when they appear.

## 3. Keeping only true facts

**A careful AI reviewer was too careful.** The first version had a second model review every fact.
It rejected good facts over small wording differences and cost about seven times more per company,
without changing what sales would do. What worked better: one reading pass that must quote its
source, plain code that checks the quote is really on the saved page, and a second pass that asks
only for what is still missing.

**Exact quotes were too strict, loose quotes too risky.** Accepting a quote when at least 80% of its
words are on the page kept real facts and still caught invented ones. Numbers, phones and e-mails
must appear in the quote itself.

**The model fell into the same traps repeatedly**, so code now catches them: parking spaces counted
as flats, Berlin-and-Brandenburg totals read as Berlin, group totals read as one company, smoke
detectors read as heat meters, old jobs read as current ones.

**Hand checks found what rules missed.** Sampling the top tiers showed cleaning, caretaker and
letting firms whose pages "look after residential buildings". A keyword rule plus a quick hand check
moved them out of the call list.

## 4. Scoring what sales cares about

**One score hid the real question.** A large manager with only an info@ address looked as good as one
with a named property manager on a direct line. Splitting the score fixed this:

- **Fit (0–100):** is this a good customer? Manages apartments +25, portfolio size up to +15 (1,000+
  apartments = key account), meters still read by hand +25 (must be remote-readable by 1 Jan 2027),
  provider change announced +25, current billing known +10.
- **Reach (0–100):** can sales get to the person who runs the buildings? Property manager before
  owner before other staff; a direct line beats a name; a personal LinkedIn profile adds 10.

Tiers come from fit: A (50+) call first, B (25–49) good prospect, C weaker fit or not yet researched.
Berlin is a requirement for the list, not a score.

## 5. Finding the right person

**Websites mostly give the managing director and info@.** That is a start, but rarely the property
manager who would champion Heidi.

**LinkedIn worked once the search was pointed at it.** A general web search returned no LinkedIn
results at all; limiting the search to linkedin.com did. Profile pages themselves can't be opened
without logging in, so only the search result (name, company, sometimes a title) is used. Result:
named decision-makers for 516 → 745 leads, 301 best contacts with a LinkedIn profile, and 222 fewer
leads stuck at info@. Job titles are often missing, and personal e-mails are rare.

**Job ads were a better buying signal than contact source.** LinkedIn's public job search showed
which leads are hiring property staff (40), a sign of a growing or stretched team. The contacts named
in ads were mostly recruiters, so they rarely became the best contact.

## 6. Making it usable and cheap

**Rebuildable outputs.** Everything is stored once in a local database with the saved pages, so the
workbook and dashboard can be rebuilt in seconds whenever a rule changes, with no new AI calls.

**Excluding early saves money.** Brokers and metering competitors are set aside before any paid step,
and paid steps run only on companies still in the list. The whole Berlin run came to about $0.09 per
lead.

## Sources

| Source | Gives |
|---|---|
| Google Places API (Text Search) | Names, address, office phone, website, map position |
| OpenStreetMap (Overpass) | Names, address, website |
| VDIV Berlin-Brandenburg members | Official names, warm introduction |
| BBU, WBGD, BVI, state-owned housing companies | Housing companies, cooperatives, managers |
| Company websites (incl. PDFs) | All facts and most contacts |
| Claude WebSearch | Websites for firms without one; LinkedIn results; team pages |
| LinkedIn public job search | Hiring signal and the ad's contact person |

## Cost per step

| Step | Cost |
|---|---|
| Discovery and crawl | Free (Google Places within the free allowance) |
| Reading pages and extracting facts | about $0.018 per company |
| Website search | about $0.043 per firm without a website |
| Contact step | about $0.05 per lead |
| Job ads | search free; about $0.003 per ad read |
| Scoring, export, dashboard | Free |

## What would come next

- Job titles from a licensed LinkedIn data provider, and verified e-mail addresses.
- Weekly re-checks for hiring and provider changes; learn the score weights from CRM outcomes.
- Other cities: map search by city and postcode, regional directories; re-read only changed pages.
