"""Independent stage screening and the dependencies of portable stage exports."""
from __future__ import annotations

import copy
import hashlib
import json

STAGES = ('cleaning', 'alignment', 'review')
STAGE_LABELS = dict(zip(STAGES, ('点云清理', '坐标对齐', '测量复核')))
DECISIONS = {'keep', 'pending', 'reject'}


def stage_version(result, stage):
    record = result.get('processing', {}).get('record', {})
    version = {'cleaning_digest': record.get('cleaning_digest', '') if record.get('removed_points', 0) else ''}
    if stage != 'cleaning':
        version['alignment_reference_id'] = result.get('alignment_reference_id', '') if result.get('has_alignment') else ''
    if stage == 'review':
        m = record.get('measurement', {})
        values = {k: m.get(k) for k in ('feature_settings', 'feature_quality', 'feature_reviews')}
        version['measurement_digest'] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return version


def stage_records(result):
    """An old shared decision belongs only to the stage at which it was made."""
    review = result.get('cloud_review') or {}
    if 'stages' in review:
        return copy.deepcopy(review['stages'])
    if review.get('schema_version') != 2 or not isinstance(review.get('processing_version'), dict):
        return {}
    old = review.get('processing_version', {})
    stage = 'alignment' if old.get('alignment_reference_id') else 'cleaning'
    return {stage: {k: review.get(k) for k in ('decision', 'reviewed_at')} | {
        'version': {k: old.get(k, '') for k in stage_version(result, stage)}}}


def workflow_state(result):
    records = stage_records(result)
    record = result.get('processing', {}).get('record', {})
    m = record.get('measurement', {})
    measured = bool(result.get('has_alignment') and m.get('reference_id') == result.get('alignment_reference_id')
                    and m.get('cleaning_digest') == record.get('cleaning_digest')
                    and (bool(m.get('measurement_started')) or bool(m.get('feature_settings'))
                         or bool(m.get('feature_reviews')) or bool(m.get('slice_reviews'))))
    outputs = {'cleaning': True, 'alignment': bool(result.get('has_alignment')), 'review': measured}
    stages, blocked_by = {}, ''
    for stage in STAGES:
        saved = records.get(stage)
        version = stage_version(result, stage)
        stale = bool(saved and saved.get('version') != version)
        decision = saved['decision'] if saved and not stale else 'pending'
        stages[stage] = {'decision': decision, 'stale': stale, 'version': version,
                         'reviewed': bool(saved and not stale), 'has_output': outputs[stage],
                         'blocked_by': blocked_by,
                         'available': not blocked_by and (stage != 'review' or bool(result.get('has_alignment')))}
        if not blocked_by and decision == 'reject':
            blocked_by = stage
    return stages


def stage_export_plan(result, selected, decisions):
    stages = workflow_state(result)
    return {stage: stages[stage] for stage in STAGES if stage in selected
            and not stages[stage]['blocked_by']
            and (stages[stage]['has_output'] or stages[stage]['reviewed'])
            and stages[stage]['decision'] in decisions}


def final_decision(stages):
    return next(reversed(stages.values()))['decision'] if stages else 'pending'
