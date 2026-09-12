"""Management-only API. Explicit draft model endpoint; no sends; every mutation pins UI account epoch."""
from functools import wraps
from flask import Blueprint, jsonify, request
from core import account_session as sessions, personalization as p, contacts, distill

bp = Blueprint('personalization', __name__)


def guarded(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            with sessions.bind() as token:
                body = request.get_json(silent=True) or {}
                if request.method != 'GET' and body.get('session') != token:
                    raise sessions.StaleAccount('页面账号已变化，请重新加载；未保存本次操作')
                result = fn(body, *args, **kwargs)
                sessions.check(token)
                return jsonify(dict(result, session=token))
        except (p.Conflict, sessions.StaleAccount) as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, TypeError, KeyError):
            return jsonify(error='参数或配置无效，请刷新后核对'), 400
        except Exception:
            # Avoid private content, filenames and credentials in error responses.
            return jsonify(error='数据暂不可用；任务保留，可稍后恢复'), 503
    return wrapper


def _contact(body):
    cid = body.get('contact') or request.args.get('contact')
    valid = {r['username'] for r in contacts.list_contacts() + contacts.list_groups()}
    if cid not in valid or cid == '__global__':
        raise ValueError('联系人不属于当前账号')
    return cid


@bp.get('/api/personalization')
@guarded
def catalog(body):
    from core import bot
    return dict(session=sessions.capture(), contacts=contacts.list_contacts() + contacts.list_groups(),
                personas=distill.list_personas(), fields=p.FIELDS, labels=p.LABELS,
                global_config=p.get('__global__'), legacy=p.legacy_inventory(bot.load_rules()), jobs=p.jobs(),
                model_drafts=True,
                default_template=p.get('__default_template__'),
                extraction='仅提取明确的长期表达反馈，资料不足保持未知；模型调用数 0')


@bp.get('/api/personalization/contact')
@guarded
def detail(body):
    cid = _contact(body)
    resolved = p.resolve_persona(cid)
    # Do not send persona samples/history or full profile facts to the local preview.
    resolved = {k: v for k, v in resolved.items() if k != 'persona'} | {'name': resolved['persona'].get('name', '角色')}
    from core import conversation_state, profile_drafts
    return dict(config=p.get(cid), effective=resolved, audit=p.audit(cid),
                conversation=conversation_state.get(cid), analysis_jobs=profile_drafts.list_jobs(cid))


@bp.post('/api/personalization/contact')
@guarded
def save(body):
    return dict(config=p.update(_contact(body), body.get('patch', {}), body.get('revision')))


@bp.post('/api/personalization/template')
@guarded
def save_default_template(body):
    return dict(default_template=p.save_template(_contact(body), body.get('revision'), body.get('enabled', True)))


@bp.post('/api/personalization/template/apply')
@guarded
def apply_default_template(body):
    return dict(config=p.apply_template(_contact(body), body.get('revision'), body.get('template_revision')))


@bp.post('/api/personalization/global')
@guarded
def global_save(body):
    from core import bot
    if body.get('accept_migration') is not True:
        raise ValueError('必须明确核对迁移预览')
    return dict(config=p.set_global(body.get('persona_id'), body.get('revision'), bot.load_rules()))


@bp.post('/api/personalization/preview')
@guarded
def prompt_preview(body):
    cid = _contact(body)
    resolved = p.resolve_persona(cid)
    # Structural/redacted preview: no persona body, private chat, names or fact data.
    chosen = p.selected_preferences(cid, str(body.get('query', ''))[:1000])
    strategies = [dict(field=x['field'], value=x['value'], category='strategy') for x in p.selected_strategies(cid, str(body.get('query', ''))[:1000])]
    safe = [dict(field=x['field'], value=x['value'] if p.FIELDS[x['field']] else '[自由文本已脱敏]',
                 source=x['source'], locked=x.get('locked', False)) for x in chosen]
    return dict(source=resolved['source'], error=resolved['error'], preference_count=len(safe), selected_strategies=strategies,
                role_name=resolved['persona'].get('name', '角色'), selected_preferences=safe,
                context=[p.BEHAVIOR, '【本轮机器人角色】[角色正文已脱敏，实际角色见 role_name]',
                         '【当前相关交流偏好】' + '；'.join(p.LABELS[x['field']] + '：' + x['value'] for x in safe) if safe else '[本轮未选入偏好]',
                         '【可尝试策略，非确定偏好】' + '；'.join(x['value'] for x in strategies) if strategies else '[本轮未选入策略]',
                         '[仅当前作用域相关事实和近期对话；本预览未读取]', '[当前请求；本预览未发送]'],
                model_calls=0, sends=0)



@bp.post('/api/personalization/history/preview')
@guarded
def history_preview(body):
    chosen = body.get('contacts', [])
    if not isinstance(chosen, list) or not 1 <= len(chosen) <= 10 or len(set(chosen)) != len(chosen):
        raise ValueError('每批选择 1–10 位联系人')
    return dict(previews=[p.preview_history(_contact({'contact': cid}), body.get('limit', 100)) for cid in chosen],
                contact_count=len(chosen), model_calls=0)


@bp.post('/api/personalization/history/create')
@guarded
def history_create(body):
    return dict(id=p.create_job(_contact(body), body['lo'], body['hi'], body['total']))


@bp.post('/api/personalization/history/step')
@guarded
def history_step(body):
    return dict(job=p.step_job(body['id']))


@bp.post('/api/personalization/analysis/preview')
@guarded
def analysis_preview(body):
    from core import profile_drafts as d
    return d.preview(_contact(body), body['start'], body['end'], body.get('segments',6), body.get('max_calls',3), body.get('token_budget',60000))


@bp.get('/api/personalization/analysis/detail')
@guarded
def analysis_detail(body):
    from core import profile_drafts as d
    return d.detail(request.args.get('id',''))


@bp.post('/api/personalization/analysis/run')
@guarded
def analysis_run(body):
    from core import profile_drafts as d
    return d.run_batch(body['id'],body['ordinal'],body.get('confirm_model_call') is True,body.get('retry_unknown') is True)


@bp.post('/api/personalization/analysis/review')
@guarded
def analysis_review(body):
    from core import profile_drafts as d
    return d.review(body['item_id'],body['action'],body['revision'],body.get('value'))


@bp.post('/api/personalization/analysis/undo')
@guarded
def analysis_undo(body):
    from core import profile_drafts as d
    return d.undo(body['application_id'],body['revision'])
