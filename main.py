import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime
import requests
import re
import time
from html import escape

# 1. 페이지 설정
st.set_page_config(page_title="우리집 가계부", layout="wide")

# --- [설정 정보] ---
NOTION_TOKEN = st.secrets["NOTION_TOKEN"]
DATABASE_ID = st.secrets["DATABASE_ID"]

CATEGORY_MAP = st.secrets["category_map"]

MONTHLY_PLAN_MAP = st.secrets["monthly_plan_map"]

FIXED_REGION_CARD_ID = st.secrets["FIXED_REGION_CARD_ID"]
FIXED_REGION_CARD_NAME = "지역카드 충전"
INPUT_SOURCE = "Python"
PAYMENT_METHODS = ["현대카드", "삼성카드", "롯데카드", "지역카드", "계좌이체"]
PERSONNEL = ["유하", "홍옥", "공동"]


@st.cache_resource
def get_worksheet():    
    gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
    sh = gc.open("PythonTest")
    return sh.get_worksheet(0)


ws = get_worksheet()


# --- [함수: 구글 시트 동기화 - page_id 기반으로 수정] ---
def sync_gsheet_row(page_id, new_row=None, action="update"):
    try:
        data = ws.get_all_values()
        if not data: return
        rows = data[1:]
        target_idx = -1

        # 시트의 K열(11번째 열)에 저장된 page_id를 검색
        for i, r in enumerate(rows):
            # r[10]은 시트의 11번째 열(K열)을 의미하며, 여기에 page_id가 저장되어 있음
            if len(r) >= 11 and r[10].strip() == str(page_id).strip():
                target_idx = i + 2
                break

        if target_idx != -1:
            if action == "update" and new_row is not None:
                new_memo_val = str(new_row["메모"]).strip() if pd.notna(new_row["메모"]) else ""
                update_values = [
                    str(new_row["날짜"]), str(new_row["지출처"]), new_memo_val,
                    int(new_row["지출"]), str(new_row["카테고리"]), str(new_row["월별가계부"]),
                    str(new_row["결제방법"]), str(new_row["인원"]), FIXED_REGION_CARD_NAME, INPUT_SOURCE, page_id
                ]
                # 찾은 target_idx 행에 새로운 데이터를 덮어씀
                ws.update(range_name=f"A{target_idx}:K{target_idx}", values=[update_values])
            elif action == "delete":
                ws.delete_rows(target_idx)
    except Exception as e:
        st.error(f"❌ 구글 시트 동기화 오류: {e}")


def insert_to_notion(date, source, memo, expense, category_id, month_id, payment, person):
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Content-Type": "application/json",
               "Notion-Version": "2022-06-28"}
    memo_val = str(memo).strip() if memo and pd.notna(memo) else ""
    properties = {
        "날짜": {"date": {"start": str(date)}},
        "수입/지출처": {"title": [{"text": {"content": str(source)}}]},
        "메모": {"rich_text": [{"text": {"content": memo_val}}]},
        "지출": {"number": int(expense) if expense else 0},
        "카테고리": {"relation": [{"id": category_id}]},
        "월별가계부": {"relation": [{"id": month_id}]},
        "결제방법": {"select": {"name": str(payment)}},
        "인원": {"select": {"name": str(person)}},
        "입력경로": {"select": {"name": INPUT_SOURCE}},
        "지역카드 충전": {"relation": [{"id": FIXED_REGION_CARD_ID}]}
    }
    data = {"parent": {"database_id": DATABASE_ID}, "properties": properties}
    return requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)


def delete_notion_page(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"}
    return requests.patch(url, headers=headers, json={"archived": True})


def update_notion_page(page_id, updated_properties):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Content-Type": "application/json",
               "Notion-Version": "2022-06-28"}
    return requests.patch(url, headers=headers, json={"properties": updated_properties})


def fetch_notion_data():
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    payload = {"sorts": [{"timestamp": "created_time", "direction": "descending"}]}
    res = requests.post(url, headers=headers, json=payload)

    if res.status_code != 200: return pd.DataFrame()
    all_pages = res.json().get("results", [])
    inv_cat = {v.replace("-", ""): k for k, v in CATEGORY_MAP.items()}
    inv_month = {v.replace("-", ""): k for k, v in MONTHLY_PLAN_MAP.items()}
    rows = []
    for page in all_pages:
        p = page["properties"]
        page_id = page["id"]
        try:
            cat_ids = [r["id"].replace("-", "") for r in p.get("카테고리", {}).get("relation", [])]
            mon_ids = [r["id"].replace("-", "") for r in p.get("월별가계부", {}).get("relation", [])]
            rows.append({
                "page_id": page_id,
                "날짜": p.get("날짜", {}).get("date", {}).get("start", "") if p.get("날짜", {}).get("date") else "",
                "지출처": p.get("수입/지출처", {}).get("title", [{}])[0].get("text", {}).get("content", "") if p.get("수입/지출처",{}).get("title") else "",
                "지출": p.get("지출", {}).get("number", 0) or 0,
                "카테고리": inv_cat.get(cat_ids[0], "미지정") if cat_ids else "미지정",
                "월별가계부": inv_month.get(mon_ids[0], "미지정") if mon_ids else "미지정",
                "결제방법": p.get("결제방법", {}).get("select", {}).get("name", "") if p.get("결제방법", {}).get("select") else "",
                "인원": p.get("인원", {}).get("select", {}).get("name", "") if p.get("인원", {}).get("select") else "",
                "입력경로": p.get("입력경로", {}).get("select", {}).get("name", "") if p.get("입력경로", {}).get("select") else "",
                "메모": p.get("메모", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "") if p.get("메모",{}).get("rich_text") else ""
            })
        except:
            continue
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(by=["날짜"], ascending=[True]).reset_index(drop=True)
    return df


menu = st.sidebar.radio("메뉴", ["지출내역 등록", "지출내역 조회"])

# --- [메뉴 1: 지출내역 등록] ---
if menu == "지출내역 등록":
    st.title("📝 지출내역 등록")
    if "form_key" not in st.session_state: st.session_state.form_key = 0
    if "show_success_balloons" not in st.session_state: st.session_state.show_success_balloons = False
    if st.session_state.show_success_balloons:
        st.balloons()
        st.success("✅ 전송이 성공적으로 완료되었습니다!")
        st.session_state.show_success_balloons = False

    with st.form(key=f"input_form_{st.session_state.form_key}"):
        input_date = st.date_input("📅 날짜", value=datetime.now())
        source = st.text_input("📍 지출처", placeholder="예: 피자콜 충주점")
        expense_raw = st.text_input("💸 지출 금액", placeholder="숫자만 입력해 주세요")
        selected_category = st.selectbox("📂 카테고리", list(CATEGORY_MAP.keys()))
        selected_payment = st.selectbox("💳 결제방법", PAYMENT_METHODS)
        selected_person = st.selectbox("👥 인원", PERSONNEL)
        memo = st.text_area("📝 메모")

        if st.form_submit_button("🚀 완료", width="stretch"):
            calc_month_str = input_date.strftime("%Y.%m")
            if not source or not expense_raw.isdigit():
                st.error("❌ 입력값을 확인해주세요.")
            else:
                with st.status("데이터 전송 중..."):
                    # 노션 저장 후 생성된 ID 받아오기
                    res = insert_to_notion(input_date.strftime("%Y-%m-%d"), source, memo, int(expense_raw),
                                           CATEGORY_MAP[selected_category], MONTHLY_PLAN_MAP[calc_month_str],
                                           selected_payment, selected_person)

                    new_page_id = res.json().get("id") if res.status_code == 200 else ""

                    # 구글 시트에 page_id를 포함하여 저장 (K열)
                    ws.append_row([input_date.strftime("%Y-%m-%d"), source, memo, int(expense_raw), selected_category,
                                   calc_month_str, selected_payment, selected_person, FIXED_REGION_CARD_NAME,
                                   INPUT_SOURCE, new_page_id])

                    st.session_state.form_key += 1
                    st.session_state.show_success_balloons = True
                    st.rerun()

# --- [메뉴 2: 지출내역 조회] ---
elif menu == "지출내역 조회":
    st.title("🔍 지출내역 조회 및 관리")
    st.info("💡 수정 후 하단 저장 버튼을 눌러주세요. (삭제는 왼쪽 삭제 칸을 체크 후 저장하세요)")

    if st.button("🔄 데이터 새로고침"):
        st.cache_resource.clear()
        st.cache_data.clear()

    df = fetch_notion_data()
    if not df.empty:
        st.markdown("### 🎯 필터링")
        c1, c2, c3 = st.columns(3)

        # 현재 월(예: 2026.04)을 가져옴
        current_month_str = datetime.now().strftime("%Y.%m")
        month_options = list(MONTHLY_PLAN_MAP.keys())

        # 현재 월이 리스트에 있으면 그 인덱스를, 없으면 0번 인덱스를 기본값으로 설정
        try:
            default_month_idx = month_options.index(current_month_str)
        except ValueError:
            default_month_idx = 0

        with c1:
            selected_month = st.selectbox("📅 조회 월", month_options, index=default_month_idx)
        with c2:
            selected_pay = st.selectbox("💳 결제방법", ["전체"] + PAYMENT_METHODS)
        with c3:
            selected_person = st.selectbox("👥 인원", ["전체"] + PERSONNEL)

        filtered_df = df[(df["입력경로"] == INPUT_SOURCE) & (df["월별가계부"] == selected_month)].copy()
        if selected_pay != "전체": filtered_df = filtered_df[filtered_df["결제방법"] == selected_pay]
        if selected_person != "전체": filtered_df = filtered_df[filtered_df["인원"] == selected_person]

        if not filtered_df.empty:
            filtered_df["날짜"] = pd.to_datetime(filtered_df["날짜"]).dt.date
            filtered_df.insert(0, "삭제", False)
            display_order = ["삭제", "날짜", "지출처", "메모", "지출", "카테고리", "월별가계부", "결제방법", "인원", "page_id"]
            filtered_df = filtered_df[display_order]

            st.divider()
            m1, m2 = st.columns(2)
            m1.metric("건수", f"{len(filtered_df)} 건")
            m2.metric("총 지출", f"{filtered_df['지출'].sum():,} 원")

            edited_df = st.data_editor(
                filtered_df,
                column_config={
                    "삭제": st.column_config.CheckboxColumn("삭제", width=30),
                    "page_id": None,  # 화면엔 안 보이지만 데이터엔 존재함
                    "날짜": st.column_config.DateColumn("날짜", width=80, format="YYYY-MM-DD"),
                    "지출처": st.column_config.TextColumn("지출처", width=200),
                    "메모": st.column_config.TextColumn("메모", width=200),
                    "지출": st.column_config.NumberColumn("지출", width=80, format="%,d"),
                    "카테고리": st.column_config.SelectboxColumn("카테고리", width=180, options=list(CATEGORY_MAP.keys())),
                    "월별가계부": st.column_config.SelectboxColumn("월별가계부", width=100,options=list(MONTHLY_PLAN_MAP.keys())),
                    "결제방법": st.column_config.SelectboxColumn("결제방법", width=100, options=PAYMENT_METHODS),
                    "인원": st.column_config.SelectboxColumn("인원", width=60, options=PERSONNEL),
                },
                num_rows="fixed",
                width="stretch",
                hide_index=True,
                key="view_edit_grid"
            )

            if st.button("💾 변경사항 저장", type="primary", width="stretch"):
                with st.status("동기화 중..."):
                    # 1. 삭제 로직
                    to_delete = edited_df[edited_df["삭제"] == True]
                    for _, d_row in to_delete.iterrows():
                        p_id = d_row["page_id"]
                        delete_notion_page(p_id)
                        sync_gsheet_row(p_id, action="delete")

                    # 2. 수정 로직
                    remaining_df = edited_df[edited_df["삭제"] == False]
                    for i in range(len(remaining_df)):
                        row = remaining_df.iloc[i]
                        p_id = row.get("page_id")
                        if pd.notna(p_id):
                            old_row = filtered_df[filtered_df["page_id"] == p_id].iloc[0]

                            row_cmp = row.copy()
                            if not isinstance(row_cmp["날짜"], str):
                                row_cmp["날짜"] = row_cmp["날짜"].strftime("%Y-%m-%d")

                            is_changed = (
                                    str(row_cmp["날짜"]) != str(old_row["날짜"]) or
                                    str(row_cmp["지출처"]) != str(old_row["지출처"]) or
                                    str(row_cmp["메모"]) != str(old_row["메모"]) or
                                    int(row_cmp["지출"]) != int(old_row["지출"]) or
                                    str(row_cmp["카테고리"]) != str(old_row["카테고리"]) or
                                    str(row_cmp["월별가계부"]) != str(old_row["월별가계부"]) or
                                    str(row_cmp["결제방법"]) != str(old_row["결제방법"]) or
                                    str(row_cmp["인원"]) != str(old_row["인원"])
                            )

                            if is_changed:
                                update_props = {
                                    "수입/지출처": {"title": [{"text": {"content": str(row["지출처"])}}]},
                                    "지출": {"number": int(row["지출"])},
                                    "메모": {"rich_text": [{"text": {"content": str(row["메모"])}}]},
                                    "결제방법": {"select": {"name": str(row["결제방법"])}},
                                    "인원": {"select": {"name": str(row["인원"])}},
                                    "카테고리": {"relation": [{"id": CATEGORY_MAP.get(row["카테고리"])}]},
                                    "월별가계부": {"relation": [{"id": MONTHLY_PLAN_MAP.get(row["월별가계부"])}]},
                                    "날짜": {"date": {"start": row_cmp["날짜"]}}
                                }
                                update_notion_page(p_id, update_props)
                                # [핵심] page_id를 기준으로 시트 행을 찾아 업데이트
                                sync_gsheet_row(p_id, new_row=row_cmp, action="update")

                    st.success("✅ 처리가 완료되었습니다!")
                    st.cache_data.clear()
                    st.rerun()
        else:
            st.warning("해당 월의 데이터가 없습니다.")
    else:
        st.info("데이터를 불러오는 중이거나 데이터가 없습니다.")
