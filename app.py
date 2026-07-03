import streamlit as st
import pandas as pd
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

# [정합성 조항] 페이지 설정은 반드시 스크립트의 최상단에서 최초 1회만 호출되어야 함
st.set_page_config(layout="wide")

# ==========================================
# [데이터베이스 레이어] 클라우드 PostgreSQL 연결 및 초기화
# ==========================================
def get_cloud_db_connection():
    """Streamlit Secrets에 저장된 자격증명을 통해 클라우드 DB에 연결"""
    return psycopg2.connect(
        host=st.secrets["db"]["host"],
        database=st.secrets["db"]["database"],
        user=st.secrets["db"]["user"],
        password=st.secrets["db"]["password"],
        port=st.secrets["db"]["port"]
    )

def initialize_cloud_database():
    """클라우드 DB 내에 표준 문서 관리 시스템 테이블 구조 생성"""
    conn = get_cloud_db_connection()
    cursor = conn.cursor()
    
    # 부품 마스터 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS part_master (
            part_no TEXT PRIMARY KEY,
            part_name TEXT NOT NULL,
            current_rev TEXT NOT NULL,
            part_type_code TEXT NOT NULL,
            registered_at TIMESTAMP NOT NULL
        )
    ''')
    
    # 초기 부품표(BOM) 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS preliminary_bom (
            bom_entry_id TEXT PRIMARY KEY,
            parent_part_no TEXT NOT NULL REFERENCES part_master(part_no),
            child_part_no TEXT NOT NULL REFERENCES part_master(part_no),
            bom_level INTEGER NOT NULL,
            quantity NUMERIC NOT NULL
        )
    ''')
    
    # 품질 특성 통제 테이블
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS control_plan_core_draft (
            id SERIAL PRIMARY KEY,
            part_no TEXT NOT NULL REFERENCES part_master(part_no),
            char_id TEXT NOT NULL,
            char_type TEXT NOT NULL,
            dimension TEXT NOT NULL,
            upper_tolerance NUMERIC NOT NULL,
            lower_tolerance NUMERIC NOT NULL
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

# ==========================================
# [비즈니스 로직 레이어] 데이터 처리 및 영속화
# ==========================================
def execute_drawing_parsing_pipeline(file_name):
    """도면 데이터 해석 알고리즘 (AI 구조체 정의)"""
    return {
        "part_no": "UCT-2026-YR310307",
        "part_name": "SHAFT-GEAR",
        "revision": "Rev.0",
        "part_type_code": "SA",
        "preliminary_bom": [
            {"child_part_no": "SUB-YR310307-01", "part_name": "PINION-GEAR", "bom_level": 1, "quantity": 1.0, "type": "MP"},
            {"child_part_no": "SUB-YR310307-02", "part_name": "BEARING-RAW", "bom_level": 1, "quantity": 2.0, "type": "PP"}
        ],
        "quality_characteristics": [
            {"char_id": "SC-1", "char_type": "Critical", "dimension": "Ø 35.00", "upper_tolerance": 0.02, "lower_tolerance": -0.01},
            {"char_id": "SC-2", "char_type": "Major", "dimension": "120.50", "upper_tolerance": 0.10, "lower_tolerance": -0.10}
        ]
    }

def commit_quality_master_data(metadata, df_characteristics):
    """클라우드 DB에 트랜잭션 단위로 최종 데이터 잠금(Lock) 및 커밋 수행"""
    conn = get_cloud_db_connection()
    cursor = conn.cursor()
    timestamp = datetime.now()
    
    try:
        # 1. 상위 모부품 등록
        cursor.execute('''
            INSERT INTO part_master (part_no, part_name, current_rev, part_type_code, registered_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (part_no) DO UPDATE SET part_name = EXCLUDED.part_name, current_rev = EXCLUDED.current_rev
        ''', (metadata["part_no"], metadata["part_name"], metadata["revision"], metadata["part_type_code"], timestamp))
        
        # 2. 하위 자부품 및 BOM 연계 적재
        for child in metadata["preliminary_bom"]:
            cursor.execute('''
                INSERT INTO part_master (part_no, part_name, current_rev, part_type_code, registered_at)
                VALUES (%s, %s, 'Rev.0', %s, %s)
                ON CONFLICT (part_no) DO NOTHING
            ''', (child["child_part_no"], child["part_name"], child["type"], timestamp))
            
            bom_entry_id = f"{metadata['part_no']}_{child['child_part_no']}"
            cursor.execute('''
                INSERT INTO preliminary_bom (bom_entry_id, parent_part_no, child_part_no, bom_level, quantity)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (bom_entry_id) DO NOTHING
            ''', (bom_entry_id, metadata["part_no"], child["child_part_no"], child["bom_level"], child["quantity"]))
            
        # 3. 기존 품질 특성 갱신
        cursor.execute('DELETE FROM control_plan_core_draft WHERE part_no = %s', (metadata["part_no"],))
        
        for _, row in df_characteristics.iterrows():
            cursor.execute('''
                INSERT INTO control_plan_core_draft (part_no, char_id, char_type, dimension, upper_tolerance, lower_tolerance)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (metadata["part_no"], row["특성 코드"], row["분류"], row["측정 규격"], row["상한 공차"], row["하한 공차"]))
            
        conn.commit()
        return True, "클라우드 데이터베이스 동기화 완료 및 1단계 잠금 완료."
    except Exception as e:
        conn.rollback()
        return False, f"클라우드 저장 실패 (Rollback): {str(e)}"
    finally:
        cursor.close()
        conn.close()

# ==========================================
# [인터페이스 레이어] 서비스 구동
# ==========================================
try:
    initialize_cloud_database()
except Exception as e:
    st.error(f"데이터베이스 연결 실패: {str(e)}. Secrets 설정을 확인하십시오.")

st.title("표준 문서 관리 시스템 (클라우드 환경)")

uploaded_drawing = st.file_uploader("고객사 수령 도면 파일을 업로드하십시오.", type=["pdf", "png", "jpg"])

if uploaded_drawing is not None:
    dataset = execute_drawing_parsing_pipeline(uploaded_drawing.name)
    
    st.markdown("### 1.1. 도면 표제란 분석 결과 (클라우드 마스터)")
    h_col1, h_col2, h_col3 = st.columns(3)
    with h_col1: st.text_input("부품 번호", value=dataset["part_no"], disabled=True)
    with h_col2: st.text_input("부품 명칭", value=dataset["part_name"], disabled=True)
    with h_col3: st.text_input("개정 차수", value=dataset["revision"], disabled=True)

    st.markdown("### 1.2. 초기 부품표(BOM) 트리 구조")
    df_bom = pd.DataFrame(dataset["preliminary_bom"])
    st.table(df_bom)

    st.markdown("### 1.3. 품질 특성 편집 및 검증")
    core_chart_structure = {
        "특성 코드": [char["char_id"] for char in dataset["quality_characteristics"]],
        "분류": [char["char_type"] for char in dataset["quality_characteristics"]],
        "측정 규격": [char["dimension"] for char in dataset["quality_characteristics"]],
        "상한 공차": [char["upper_tolerance"] for char in dataset["quality_characteristics"]],
        "하한 공차": [char["lower_tolerance"] for char in dataset["quality_characteristics"]]
    }
    edited_df_core = st.data_editor(pd.DataFrame(core_chart_structure), num_rows="dynamic", use_container_width=True)
    
    if st.button("데이터 최종 확정 및 Lock (Sign-off)", use_container_width=True):
        success, message = commit_quality_master_data(dataset, edited_df_core)
        if success: st.success(message)
        else: st.error(message)
