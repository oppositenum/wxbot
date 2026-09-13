"""Owner-only Moments management. Draft creation does not authorize sending."""
import io
import json
from functools import wraps

from flask import Blueprint, jsonify, request, send_file
from core import account_session as sessions, moments as m


def create_blueprint(authorize):
    bp = Blueprint('moments', __name__)

    @bp.before_request
    def protect():
        if not authorize():
            return jsonify(error='朋友圈管理需要管理员授权'), 403
        if request.content_length and request.content_length > m.MAX_IMAGE_BYTES + 65536:
            return jsonify(error='上传内容过大'), 413

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def guarded(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            try:
                with sessions.bind() as token:
                    body = request.get_json(silent=True) or {}
                    if not isinstance(body, dict):
                        raise ValueError('请求格式无效')
                    expected = body.get('session')
                    if request.mimetype == 'multipart/form-data':
                        expected = json.loads(request.form.get('session', 'null'))
                    if request.method != 'GET' and expected != token:
                        raise sessions.StaleAccount('当前账号已变化，请重新加载；本次操作未执行')
                    if request.method == 'GET' and request.args.get('session'):
                        if json.loads(request.args['session']) != token:
                            raise sessions.StaleAccount('页面账号已变化，请重新加载')
                    result = fn(body, *args, **kwargs)
                    sessions.check(token)
                    if isinstance(result, dict):
                        return jsonify(dict(result, session=token))
                    return result
            except (sessions.StaleAccount, m.Conflict) as exc:
                return jsonify(error=str(exc)), 409
            except m.Unavailable as exc:
                return jsonify(error=str(exc), code='moments_unavailable'), 503
            except (ValueError, TypeError, KeyError):
                return jsonify(error='参数无效，请核对内容、图片和设置'), 400
            except Exception:
                return jsonify(error='朋友圈数据暂不可用，已保留草稿和上次缓存'), 503
        return run

    @bp.get('/api/moments')
    @guarded
    def catalog(body):
        return dict(m.catalog(request.args.get('limit', 30), request.args.get('offset', 0),
                              request.args.get('author', ''), request.args.get('search', '')),
                    settings=m.settings(), capabilities=m.capabilities(), status=m.status(),
                    timezone='Asia/Shanghai', quiet_now=m.quiet_now(m.settings()))

    @bp.post('/api/moments/sync')
    @guarded
    def sync(body):
        from core import moments_jobs
        return dict(result=moments_jobs.refresh())

    @bp.get('/api/moments/feed/<fid>')
    @guarded
    def detail(body, fid):
        return dict(item=m.detail(fid))

    @bp.post('/api/moments/settings')
    @guarded
    def settings(body):
        return dict(settings=m.save_settings(body.get('patch'), body.get('revision')))

    @bp.get('/api/moments/drafts')
    @guarded
    def drafts(body):
        return dict(items=m.drafts())

    @bp.post('/api/moments/drafts')
    @guarded
    def save(body):
        return dict(draft=m.save_draft(body))

    @bp.post('/api/moments/ai-reply')
    @guarded
    def ai_reply(body):
        from core import moments_ai
        return moments_ai.generate(body)

    @bp.post('/api/moments/drafts/<jid>/discard')
    @guarded
    def discard(body, jid):
        return m.discard_draft(jid, body.get('revision'))

    @bp.post('/api/moments/drafts/<jid>/submit')
    @guarded
    def submit(body, jid):
        from core import moments_jobs
        return dict(job=moments_jobs.enqueue_draft(jid, body.get('revision')))

    @bp.get('/api/moments/jobs')
    @guarded
    def jobs(body):
        from core import moments_jobs
        return dict(items=moments_jobs.listing())

    @bp.post('/api/moments/jobs/<jid>/cancel')
    @guarded
    def cancel(body, jid):
        from core import moments_jobs
        return moments_jobs.cancel(jid)

    @bp.post('/api/moments/ai-post')
    @guarded
    def ai_post(body):
        from core import moments_ai
        post=moments_ai.generate_post(m.settings()['moods'])
        return dict(text=post['text'],image_prompt=post.get('image_prompt',''),sent=False)

    @bp.post('/api/moments/upload')
    @guarded
    def upload(body):
        if 'file' not in request.files:
            raise ValueError('缺少图片')
        return dict(asset=m.upload(request.files['file'].stream))

    @bp.get('/api/moments/assets/<aid>')
    @guarded
    def asset(body, aid):
        if not request.args.get('session'):
            raise sessions.StaleAccount('图片请求必须绑定当前账号')
        # Materialize before the account check, never stream a dynamically resolved path.
        data = m.asset(aid).read_bytes()
        return send_file(io.BytesIO(data), mimetype='image/jpeg', download_name='moments.jpg')

    return bp
