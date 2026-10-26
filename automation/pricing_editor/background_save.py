"""Non-UI dated-price save and projection using an isolated editor snapshot."""
from datetime import date, timedelta
from pathlib import Path


def save_editor_snapshot(state, root, key_date, baseline, displayed, label):
    try:
        from automation.pricing_timeline import PricingTimeline
        from automation.promo_planner.store import PromoStore
        from automation.promo_planner.date_price_changes import save_key_date_changes,project_dataframe,remove_key_date_overrides
    except ImportError:
        from pricing_timeline import PricingTimeline
        from promo_planner.store import PromoStore
        from promo_planner.date_price_changes import save_key_date_changes,project_dataframe,remove_key_date_overrides
    root=Path(root);timeline=PricingTimeline(root/'outputs/promo_planner/promo_schedule.db');store=PromoStore(timeline.path)
    if not any(m['source']=='price_date' for m in timeline.event_markers(key_date)):
        timeline.schedule_price_change_date(key_date,label or 'Price update')
    # Floor recalculation and whole-catalog diffing occur on this worker, never on Qt.
    edited=state.export_prices_dataframe()
    count,_=save_key_date_changes(root,key_date,edited,baseline,store.list_campaigns(),label,
                                  displayed_df=displayed,normalize_price=state.round_regular_price)
    boundary=(date.fromisoformat(key_date)+timedelta(days=1)).isoformat()
    raw=timeline.project(boundary,layers=('base','import','override'))
    view,preview=project_dataframe(raw,root/'unused.xlsx',store,key_date,root,replay_executed=True)
    clean=remove_key_date_overrides(view,root,key_date,boundary)
    # Most saves do not change the visible model. Avoid a whole-catalog UI reload in that case.
    import pandas as pd
    identity=[c for c in ("ISO","Plan","Days","GB","PricingUnitIdUsed") if c in edited and c in view]
    fields=[c for c in ("Price_EUR","Price_USD","PromoCode","PromoType","PromoValue","PromoNet_EUR","PromoNet_USD","AllowBelowCost","_ManualPALinks","_PromoPlannerPromoOwner","_PromoPlannerPAUnderlying","_PromoPlannerPAUnresolved","_PromoPlannerOriginalPrice_EUR","_PromoPlannerOriginalPrice_USD","_PromoPlannerPriceOwner","_PromoPlannerPromoStack") if c in edited or c in view]
    def comparable(frame):
        part=frame.reindex(columns=identity+fields).copy()
        if "PromoCode" in part and "PromoValue" in part:
            part.loc[part["PromoCode"].fillna("").astype(str).str.strip().eq(""), "PromoValue"] = 0.0
        for column in ("Days","GB","Price_EUR","Price_USD","PromoValue","PromoNet_EUR","PromoNet_USD","_PromoPlannerOriginalPrice_EUR","_PromoPlannerOriginalPrice_USD"):
            if column in part:part[column]=pd.to_numeric(part[column],errors="coerce").astype(float).round(7)
        return part.fillna("").astype(str).sort_values(identity).reset_index(drop=True)
    changed=not comparable(edited).equals(comparable(view))
    try:
        from automation.promo_planner.issue_review import review_summary
    except ImportError:
        from promo_planner.issue_review import review_summary
    return dict(count=count,edited=edited,view=view,baseline=clean,model_changed=changed,
                promotion_review=review_summary(preview))
