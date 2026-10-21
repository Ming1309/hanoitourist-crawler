"""Coverage never promotes an incomplete browser capture to success."""

from ..errors import capture_errors


def validate(snapshot, documents, run):
    issues = list(dict.fromkeys(snapshot.warnings))
    expected = run.get('manifest', {}).get('inventory', {})
    discovered = snapshot.coverage['discovered']
    actions = run.get('manifest', {}).get('actions', {})
    if run['capture_status'] != 'complete':
        issues.append('Browser capture was not complete')
    if not actions or any(value != 'captured' for value in actions.values()):
        issues.append('Some interaction states were not captured')
    issues.extend(capture_errors(run.get('errors', [])))
    for key in ('sections', 'days'):
        if set(expected.get(key, [])) - set(discovered[key]):
            issues.append(f'Unparsed discovered {key}')
    if any(s.state == 'missing' for s in snapshot.sections):
        issues.append('Expected tour section selector missing')
    if set(discovered['days']) - {d.label for d in snapshot.days}:
        issues.append('Itinerary day has no parsed body')
    for sid, count in {**discovered['departures'], **expected.get('departures', {})}.items():
        observations = [d for d in snapshot.departures if d.source_schedule_id == sid]
        if not observations or any(len(d.price_cells) != count for d in observations):
            issues.append(f'Missing price matrix cells for schedule {sid}')
    if not snapshot.departures:
        # Zero schedules is valid only with an explicit UI empty-result marker.
        if not any(b'view-empty' in d.body for d in documents if d.role in {'tour', 'schedule'}):
            issues.append('No schedules and no explicit empty-result marker')
    by_id = {}
    for departure in snapshot.departures:
        by_id.setdefault(departure.source_schedule_id, []).append(departure)
    for sid, observations in by_id.items():
        if len(observations) > 1:
            issues.append(f'Conflicting observations retained for schedule {sid}')
    raw_ids = {d.id for d in documents}
    observations_by_id = {d.id: d for d in snapshot.departures}
    parsed_rows = snapshot.coverage.get('parsed_schedule_rows', {})
    source_rows = {r['row_key']: r for r in discovered.get('schedule_rows', [])}
    expected_rows = {r['row_key']: r for r in expected.get('schedule_rows', [])}
    for key, record in {**expected_rows, **source_rows}.items():
        if key in expected_rows and (key not in source_rows or expected_rows[key] != record):
            issues.append(f'Missing or changed discovered schedule row {key}')
        observation = observations_by_id.get(parsed_rows.get(key))
        provenance = record.get('provenance', [])
        if observation and (not observation.departure_raw or observation.departure_at is None):
            source = provenance[0] if provenance else {}
            issues.append(f'Missing or unparsed departure date for schedule row {key}; '
                          f'raw={source.get("raw_document_id", "")}; locator={source.get("locator", "")}')
        if record.get('identity_error'):
            issues.append(f'Unrecognized schedule row {key}: {record["identity_error"]}; '
                          f'{record.get("departure_raw", "")}')
        if (not observation or record.get('identity_error')
                or observation.source_schedule_id != record.get('source_schedule_id')
                or record.get('source_tour_id') != snapshot.source_tour_id
                or observation.departure_raw != record.get('departure_raw')
                or len(observation.price_cells) != record.get('price_cells')
                or not provenance or any(p not in observation.provenance for p in provenance)):
            issues.append(f'Unparsed or incomplete schedule row {key}')
        if not provenance or any(p.get('raw_document_id') not in raw_ids or not p.get('locator') for p in provenance):
            issues.append(f'Missing or invalid schedule row provenance {key}')
    items = snapshot.sections + snapshot.days + snapshot.media + snapshot.departures
    items += [c for d in snapshot.departures for c in d.price_cells]
    for item in items:
        if getattr(item, 'state', '') == 'missing':
            continue
        if not item.provenance or any(p['raw_document_id'] not in raw_ids or not p['locator'] for p in item.provenance):
            issues.append('Missing or invalid raw provenance')
    snapshot.warnings = list(dict.fromkeys(issues))
    snapshot.status = 'partial' if snapshot.warnings else 'complete'
    snapshot.coverage.update({'distinct_schedules': len(by_id), 'observations': len(snapshot.departures),
                             'price_cells': sum(len(d.price_cells) for d in snapshot.departures),
                             'days': len(snapshot.days), 'actions': actions,
                             'capture_status': run['capture_status']})
    return snapshot
