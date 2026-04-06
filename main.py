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

@st.cache_data(ttl=600) # 10분 동안은 API 호출 없이 캐시된 데이터를 사용
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


menu = st.sidebar.radio("가계부 메뉴", ["지출내역 등록", "지출내역 조회"])

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
    st.title("🔍 지출내역 조회")
    
    # [추가] 수정 팝업 함수 (st.dialog)
    @st.dialog("📝 지출내역 수정")
    def edit_dialog(row_data):
        st.write(f"**ID:** {row_data['page_id']}")
        
        with st.form("edit_form"):
            new_date = st.date_input("📅 날짜", value=pd.to_datetime(row_data["날짜"]))
            new_source = st.text_input("📍 지출처", value=row_data["지출처"])
            new_expense = st.number_input("💸 지출 금액", value=int(row_data["지출"]), step=100)
            new_category = st.selectbox("📂 카테고리", list(CATEGORY_MAP.keys()), 
                                        index=list(CATEGORY_MAP.keys()).index(row_data["카테고리"]))
            new_payment = st.selectbox("💳 결제방법", PAYMENT_METHODS, 
                                       index=PAYMENT_METHODS.index(row_data["결제방법"]))
            new_person = st.selectbox("👥 인원", PERSONNEL, 
                                      index=PERSONNEL.index(row_data["인원"]))
            new_memo = st.text_area("📝 메모", value=row_data["메모"])
            
            submit = st.form_submit_button("💾 수정사항 저장", use_container_width=True)
            
            if submit:
                with st.status("업데이트 중..."):
                    p_id = row_data["page_id"]
                    formatted_date = new_date.strftime("%Y-%m-%d")
                    calc_month_str = new_date.strftime("%Y.%m")
                    
                    # 노션 업데이트용 프로퍼티
                    update_props = {
                        "수입/지출처": {"title": [{"text": {"content": str(new_source)}}]},
                        "지출": {"number": int(new_expense)},
                        "메모": {"rich_text": [{"text": {"content": str(new_memo)}}]},
                        "결제방법": {"select": {"name": str(new_payment)}},
                        "인원": {"select": {"name": str(new_person)}},
                        "카테고리": {"relation": [{"id": CATEGORY_MAP.get(new_category)}]},
                        "월별가계부": {"relation": [{"id": MONTHLY_PLAN_MAP.get(calc_month_str)}]},
                        "날짜": {"date": {"start": formatted_date}}
                    }
                    
                    # 1. 노션 업데이트
                    update_notion_page(p_id, update_props)
                    
                    # 2. 구글 시트 업데이트용 데이터 구성
                    new_row_for_sheet = {
                        "날짜": formatted_date,
                        "지출처": new_source,
                        "메모": new_memo,
                        "지출": new_expense,
                        "카테고리": new_category,
                        "월별가계부": calc_month_str,
                        "결제방법": new_payment,
                        "인원": new_person
                    }
                    sync_gsheet_row(p_id, new_row=new_row_for_sheet, action="update")
                    
                st.success("✅ 수정되었습니다!")
                time.sleep(1)
                st.cache_data.clear()
                st.rerun()

    if st.button("🔄 데이터 새로고침"):
        st.cache_resource.clear()
        st.cache_data.clear()
        st.rerun()

    df = fetch_notion_data()
    if not df.empty:
        st.markdown("### 🎯 필터링")
        c1, c2, c3 = st.columns(3)

        current_month_str = datetime.now().strftime("%Y.%m")
        month_options = list(MONTHLY_PLAN_MAP.keys())
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
            
            st.divider()
            m1, m2 = st.columns(2)
            m1.metric("건수", f"{len(filtered_df)} 건")
            m2.metric("총 지출", f"{filtered_df['지출'].sum():,} 원")

            # --- [수정된 그리드 영역] ---
            # 헤더 출력
            cols = st.columns([1, 2, 2, 3, 2, 1]) # 너비 비율 조정
            fields = ["관리", "날짜", "지출처", "메모", "지출", "카테고리"]
            for col, field in zip(cols, fields):
                col.write(f"**{field}**")

            # 데이터 행 반복
            for idx, row in filtered_df.iterrows():
                with st.container():
                    c1, c2, c3, c4, c5, c6 = st.columns([1, 2, 2, 3, 2, 1])
                    # 수정 버튼
                    if c1.button("📝", key=f"edit_{row['page_id']}"):
                        edit_dialog(row)
                    
                    c2.write(row["날짜"])
                    c3.write(row["지출처"])
                    c4.write(row["메모"])
                    c5.write(f"{row['지출']:,}원")
                    c6.write(row["카테고리"])
            # --------------------------
        else:
            st.warning("해당 월의 데이터가 없습니다.")
    else:
        st.info("데이터를 불러오는 중이거나 데이터가 없습니다.")
