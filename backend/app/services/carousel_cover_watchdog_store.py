"""Закрытый service_role контроллер; миграция отсутствует — выдача выключена."""
from app.services.carousel_cover_store import get_cover_db


class WatchdogStore:
    def rpc(self, name, params):
        value = get_cover_db().rpc(name, params).execute().data
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if not isinstance(value, dict):
            raise ValueError('Некорректный ответ контроллера обложек')
        return value

    def status(self):
        return self.rpc('carousel_cover_background_status', {})

    def claim(self):
        return self.rpc('carousel_cover_background_claim_tick', {})

    def finish(self, token, reason, refs, summary):
        return self.rpc('carousel_cover_background_finish_tick', {
            'p_token': token, 'p_reason': reason, 'p_refs': refs, 'p_summary': summary,
        })

    def reap(self):
        return self.rpc('reap_carousel_cover_leases', {})
