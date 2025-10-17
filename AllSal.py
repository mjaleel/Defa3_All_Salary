import streamlit as st
import pandas as pd
import io
import os
import csv
import re
from datetime import datetime
from openpyxl.workbook import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font, PatternFill
import shutil
# ملاحظة: تم إزالة استيراد threading و tkinter و customtkinter
# لأن Streamlit يدير دورة حياة التطبيق بشكل مختلف.

# ----------------------------------------------------------------------
# --- الثوابت والبيانات الثابتة ---
# ----------------------------------------------------------------------
PAYER_NAME = "مديرية تربية البصرة"
PAYER_ACCOUNT = "IQ26RAFB002100366585001"
CURRENCY = "IQD"
DETAILS_OF_CHARGES = "SLEV"
REMITTANCE_INFO_TEMPLATE = "SALARY {} {}"
MAX_ROWS_PER_FILE = 4000
MAX_AMOUNT_PER_FILE = 4_500_000_000
BANK_BICS = {
    'RAFB': 'RAFBIQB1098', 'RDBA': 'RDBAIQB1046', 'AIBI': 'AIBIIQBA991',
    'IDBQ': 'IDBQIQBA004', 'AINI': 'AINIIQBA015', 'NBIQ': 'NBIQIQBA830'
}
ARABIC_BANK_NAME_MAP = {
    'RAFB': 'الرافدين', 'RDBA': 'الرشيد', 'AIBI': 'آشور',
    'IDBQ': 'التنمية', 'AINI': 'الطيف', 'NBIQ': 'الأهلي'
}
ALL_BRANCHES_BIC = {
    'RAFBIQB1098', 'RDBAIQB1046', 'AIBIIQBA991', 'IDBQIQBA004',
    'AINIIQBA015', 'AINIIQBA009',
    'NBIQIQBA830', 'NBIQIQBA856', 'NBIQIQBA859', 'NBIQIQBA005',
    'NBIQIQBA860', 'NBIQIQBA862', 'NBIQIQBA849', 'NBIQIQBA865',
    'NBIQIQBA844', 'NBIQIQBA848', 'NBIQIQBA850'
}
BANKS_WITH_DYNAMIC_BRANCHES = ['AINI', 'NBIQ']
BANK_KEYS_FOR_FILTERING = list(BANK_BICS.keys())
ARABIC_MONTHS = {
    1: "كانون الثاني", 2: "شباط", 3: "آذار", 4: "نيسان", 5: "أيار", 6: "حزيران",
    7: "تموز", 8: "آب", 9: "أيلول", 10: "تشرين الأول", 11: "تشرين الثاني", 12: "كانون الأول"
}
FINAL_EXCEL_COLS = [
    'Reference', 'Value Date', 'Payer Name', 'Payer Acount', 'Amount',
    'Currency', 'Receiver BIC', 'Beneficiary Name', 'Beneficiary Acount',
    'Remittance Information', 'Details of Charges'
]
# --- تهيئة حالة الجلسة ---
if 'processed_files' not in st.session_state:
    st.session_state.processed_files = []
if 'summary_file' not in st.session_state:
    st.session_state.summary_file = None
if 'encrypted_files' not in st.session_state:
    st.session_state.encrypted_files = []
if 'txt_files_deleted' not in st.session_state:
    st.session_state.txt_files_deleted = False


# ----------------------------------------------------------------------
# --- الدوال المساعدة (بدون تغيير كبير في المنطق الداخلي) ---
# ----------------------------------------------------------------------

# (تم إزالة دالة adjust_column_width و set_arabic_number_format لتبسيط التوافق مع بيئة Streamlit)
# لأن التعامل مع تنسيقات openpyxl يكون معقداً داخل Streamlit ويُفضل ترك التنسيق اليدوي
# أو استخدام أدوات تخطيط البيانات في Streamlit.

def get_receiver_bic_dynamic(row):
    """تحديد BIC للمصرف بناءً على مفتاح المصرف ورقم IBAN."""
    key = row['Bank Key']
    iban = str(row['Iban'])
    if key in BANKS_WITH_DYNAMIC_BRANCHES:
        try:
            branch_code = iban[8:11]
            bic_prefix = key + 'IQBA'
            suggested_bic = bic_prefix + branch_code
            if suggested_bic in ALL_BRANCHES_BIC:
                return suggested_bic
            else:
                return BANK_BICS[key]
        except Exception:
            return BANK_BICS[key]
    else:
        return BANK_BICS[key]

# ----------------------------------------------------------------------
# --- دوال المعالجة الرئيسية (تستخدم Streamlit Caching/Status) ---
# ----------------------------------------------------------------------

# @st.cache_data(show_spinner=False) # يمكن استخدام التخزين المؤقت إذا لم يتغير ملف الإدخال
def process_excel_data_st(uploaded_file, status_container):
    """معالجة ملف الإدخال وتقسيمه إلى ملفات Excel حسب المصرف/الفرع."""
    st.session_state.processed_files = []
    
    with status_container.status("بدء المعالجة...", expanded=True) as status:
        try:
            # قراءة البيانات
            status.update(label="جاري قراءة ملف الإدخال...", state="running")
            df = pd.read_excel(uploaded_file)
            
            required_cols = ['الاسم', 'Iban', 'الراتب الصافي']
            if not all(col in df.columns for col in required_cols):
                st.error(f"الملف يجب أن يحتوي على الأعمدة: {', '.join(required_cols)}")
                status.update(label="فشل المعالجة: أعمدة مفقودة.", state="error")
                return []
            
            df['الراتب الصافي'] = pd.to_numeric(df['الراتب الصافي'], errors='coerce')
            df = df.dropna(subset=['الراتب الصافي', 'Iban', 'الاسم'])

            # فلترة وحذف الصفوف ذات الراتب الصفر
            initial_rows = len(df)
            df = df[df['الراتب الصافي'] != 0]
            rows_dropped = initial_rows - len(df)
            if rows_dropped > 0:
                st.warning(f"تم حذف **{rows_dropped}** صفاً من عمود 'الراتب الصافي' بقيمة صفر.")
            
            df['الاسم'] = df['الاسم'].astype(str).str[:35]

            today = datetime.now()
            date_str = today.strftime('%Y%m%d')
            date_ref = today.strftime('%Y%m%d')
            current_year = today.strftime('%Y')
            month_number = today.month
            current_month_arabic = ARABIC_MONTHS.get(month_number, "شهر غير محدد")

            # تجهيز الأعمدة الثابتة والمشتقة
            # ... (نفس منطق تجهيز الأعمدة) ...
            df['Value Date'] = date_str
            df['Payer Name'] = PAYER_NAME
            df['Payer Acount'] = PAYER_ACCOUNT
            df['Amount'] = df['الراتب الصافي']
            df['Currency'] = CURRENCY
            df['Details of Charges'] = DETAILS_OF_CHARGES
            df['Beneficiary Name'] = df['الاسم']
            df['Beneficiary Acount'] = df['Iban']
            remittance_info = REMITTANCE_INFO_TEMPLATE.format(current_year, current_month_arabic)
            df['Remittance Information'] = remittance_info
            df['Bank Key'] = df['Iban'].str[4:8]

            df_filtered = df[df['Bank Key'].isin(BANK_KEYS_FOR_FILTERING)].copy()
            df_filtered['Receiver BIC'] = df_filtered.apply(get_receiver_bic_dynamic, axis=1)
            df_filtered['Reference'] = date_ref + ' ' + df_filtered['Iban'].astype(str)

            df_final = df_filtered[FINAL_EXCEL_COLS]
            grouped_by_bank_bic = df_final.groupby('Receiver BIC')
            file_count = 0
            processed_files_list = []

            # --- تصدير ملفات كل بنك وفرع (في الذاكرة) ---
            for bic, bank_df in grouped_by_bank_bic:
                bank_key = bic[:4]
                arabic_bank_name = ARABIC_BANK_NAME_MAP.get(bank_key, 'مصرف_غير_معروف')
                num_rows = len(bank_df)
                start_row = 0
                file_index = 1
                
                status.update(label=f"جاري تقسيم بيانات بنك **{arabic_bank_name}** - فرع **{bic[-3:]}**...", state="running")
                
                while start_row < num_rows:
                    end_row = min(start_row + MAX_ROWS_PER_FILE, num_rows)
                    current_slice = bank_df.iloc[start_row:end_row]
                    total_amount = current_slice['Amount'].sum()

                    # منطق تقسيم الملفات حسب المبلغ الأقصى (لتبسيط الأمر، يمكن تركيز منطق التقسيم على حجم الصفوف لبيئة Streamlit)
                    # مع الحفاظ على المنطق الأصلي قدر الإمكان
                    while total_amount > MAX_AMOUNT_PER_FILE and len(current_slice) > 1:
                        end_row -= 1
                        current_slice = bank_df.iloc[start_row:end_row]
                        total_amount = current_slice['Amount'].sum()

                    if total_amount > MAX_AMOUNT_PER_FILE:
                        current_slice = bank_df.iloc[start_row:start_row+1]
                        end_row = start_row + 1

                    output_filename = f"{arabic_bank_name}_الملف_{file_index}_{bic[-3:]}_{date_str}.xlsx"
                    
                    # حفظ في الذاكرة (باستخدام io.BytesIO)
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer: # استخدام xlsxwriter لتجنب التبعيات المعقدة لـ openpyxl في هذه الخطوة
                        current_slice.to_excel(writer, index=False, sheet_name='Sheet1')
                    output.seek(0)
                    
                    processed_files_list.append({
                        'filename': output_filename,
                        'content': output.getvalue(),
                        'bank_name': arabic_bank_name,
                        'branch_code': bic[-3:],
                        'rows': len(current_slice),
                        'amount': round(total_amount, 2)
                    })

                    file_count += 1
                    status.update(label=f"تم إنشاء ملف: {output_filename}. عدد الصفوف: {len(current_slice)}.", state="running")
                    start_row = end_row
                    file_index += 1

            st.success(f"اكتملت المعالجة بنجاح. تم إنشاء **{file_count}** ملف إخراج.")
            status.update(label=f"اكتملت المعالجة بنجاح. تم إنشاء {file_count} ملف إخراج. 🎉", state="complete")
            st.session_state.processed_files = processed_files_list
            return processed_files_list

        except Exception as e:
            st.error(f"حدث خطأ أثناء المعالجة: {e}")
            status.update(label=f"حدث خطأ أثناء المعالجة: {e}", state="error")
            return []

# ----------------------------------------------------------------------

def create_summary_file_st(processed_files_list, status_container):
    """إنشاء ملف الملخص الإحصائي من قائمة الملفات المعالجة."""
    st.session_state.summary_file = None
    
    with status_container.status("بدء إنشاء ملف الملخص الإحصائي الهيكلي...", expanded=True) as status:
        if not processed_files_list:
            st.warning("لم يتم العثور على أي ملفات إخراج (Excel) لإنشاء الملخص. يرجى تشغيل المعالجة أولاً.")
            status.update(label="لم يتم العثور على ملفات معالجة.", state="error")
            return None

        summary_by_bank = {}
        key_map_reverse = {v: k for k, v in ARABIC_BANK_NAME_MAP.items()}

        try:
            for file_data in processed_files_list:
                arabic_bank_name = file_data['bank_name']
                filename = file_data['filename']
                branch_code = file_data['branch_code']
                num_employees = file_data['rows']
                total_amount = file_data['amount']

                bank_key = key_map_reverse.get(arabic_bank_name, 'Unknown')
                
                if bank_key not in summary_by_bank:
                    summary_by_bank[bank_key] = {
                        'name_ar': arabic_bank_name,
                        'files': [],
                        'total_employees': 0,
                        'total_amount': 0.0
                    }

                summary_by_bank[bank_key]['files'].append({
                    'اسم الملف': filename,
                    'رمز الفرع': branch_code,
                    'عدد المنتسبين (الصفوف)': num_employees,
                    'المبلغ الإجمالي (د.ع)': total_amount
                })

                summary_by_bank[bank_key]['total_employees'] += num_employees
                summary_by_bank[bank_key]['total_amount'] += total_amount

            # ... (نفس منطق تجميع البيانات للملخص الكامل) ...
            full_summary_data = []
            grand_total_employees = 0
            grand_total_amount = 0.0
            
            FIELD_FILE = 'اسم الملف / المصرف'
            FIELD_BRANCH = 'رمز الفرع / المفتاح'
            FIELD_COUNT = 'عدد المنتسبين (الصفوف)'
            FIELD_AMOUNT = 'المبلغ الإجمالي (د.ع)'
            
            sorted_bank_keys = sorted(summary_by_bank.keys())

            for bank_key in sorted_bank_keys:
                bank_data = summary_by_bank[bank_key]
                
                for file_data in bank_data['files']:
                    full_summary_data.append({
                        FIELD_FILE: file_data['اسم الملف'],
                        FIELD_BRANCH: file_data['رمز الفرع'],
                        FIELD_COUNT: file_data['عدد المنتسبين (الصفوف)'],
                        FIELD_AMOUNT: file_data['المبلغ الإجمالي (د.ع)']
                    })
                    
                full_summary_data.append({
                    FIELD_FILE: f"**المجموع الكلي لـ {bank_data['name_ar']}**",
                    FIELD_BRANCH: bank_key,
                    FIELD_COUNT: bank_data['total_employees'],
                    FIELD_AMOUNT: round(bank_data['total_amount'], 2)
                })
                
                full_summary_data.append({FIELD_FILE: '', FIELD_BRANCH: '', FIELD_COUNT: '', FIELD_AMOUNT: ''})
                
                grand_total_employees += bank_data['total_employees']
                grand_total_amount += bank_data['total_amount']

            full_summary_data.append({
                FIELD_FILE: "**المجموع الكلي النهائي لكافة المصارف**",
                FIELD_BRANCH: "GRAND TOTAL",
                FIELD_COUNT: grand_total_employees,
                FIELD_AMOUNT: round(grand_total_amount, 2)
            })

            df_full_summary = pd.DataFrame(full_summary_data)
            
            # حفظ الملخص في الذاكرة
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df_full_summary.to_excel(writer, index=False, sheet_name='ملخص_هيكلي_كامل')
            output.seek(0)
            
            date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            summary_output_filename = f"Summary_Report_{date_str}.xlsx"
            st.session_state.summary_file = {'filename': summary_output_filename, 'content': output.getvalue()}

            st.success(f"اكتمل إنشاء الملخص الهيكلي بنجاح. 🎉")
            status.update(label=f"اكتمل إنشاء الملخص الهيكلي بنجاح. 🎉", state="complete")
            return output.getvalue()

        except Exception as e:
            st.error(f"حدث خطأ أثناء إنشاء الملخص: {e}")
            status.update(label=f"حدث خطأ أثناء إنشاء الملخص: {e}", state="error")
            return None

# ----------------------------------------------------------------------

def batch_convert_excel_to_csv_txt_st(processed_files_list, status_container):
    """تحويل الملفات المعالجة (في الذاكرة) إلى TXT/CSV (في الذاكرة)"""
    st.session_state.encrypted_files = []
    
    with status_container.status("بدء عملية التشفير/التحويل إلى TXT/CSV (على دفعات)...", expanded=True) as status:
        if not processed_files_list:
            st.warning("لم يتم العثور على أي ملفات Excel مُعالجة لتشفيرها/تحويلها.")
            status.update(label="لم يتم العثور على ملفات معالجة.", state="error")
            return []
            
        success_count = 0
        encrypted_files_list = []
        
        try:
            for file_data in processed_files_list:
                filename = file_data['filename']
                base_name = os.path.splitext(filename)[0]
                
                status.update(label=f"معالجة الملف: **{filename}**...", state="running")
                
                # 1. قراءة الملف المقسم من الذاكرة
                df = pd.read_excel(io.BytesIO(file_data['content']), dtype=str)

                # تنسيق عمود المبلغ
                if 'Amount' in df.columns:
                    df['Amount'] = df['Amount'].astype(str).str.replace(',', '')
                    df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce').map(
                        lambda x: f'"{int(x):,}"' if pd.notnull(x) else ""
                    )

                # 2. تحضير لملف TXT (إزالة عمود Reference)
                cols_to_keep = [col for col in df.columns if col != 'Reference']
                
                # حفظ مؤقت إلى مصفوفة بايت بترميز utf-16 مفصول بـ TAB
                buffer_utf16 = io.StringIO()
                df[cols_to_keep].to_csv(buffer_utf16, sep='\t', index=False, encoding='utf-16', quoting=csv.QUOTE_NONE, escapechar='\\')
                
                # 3. قراءة المحتوى واستبدال المسافات/الـ TAB بـ | والترميز إلى UTF-8
                lines = buffer_utf16.getvalue().splitlines()
                new_lines = []
                for line in lines:
                    new_line = re.sub(r'[ \t]+', '|', line.rstrip('\n\r'))
                    new_lines.append(new_line)
                    
                # إزالة السطر الأول (رؤوس الأعمدة)
                if new_lines:
                    new_lines.pop(0)

                # 4. حفظ الملف النهائي بترميز UTF-8 في الذاكرة
                final_content = '\n'.join(new_lines).encode('utf-8')
                
                # 5. حفظ الملفات الناتجة (TXT و CSV) في قائمة الذاكرة
                unicode_txt_filename = base_name + ".txt"
                csv_filename = base_name + ".csv"
                
                encrypted_files_list.append({'filename': unicode_txt_filename, 'content': final_content})
                # ملف CSV هو نسخة طبق الأصل من ملف TXT في هذه الحالة
                encrypted_files_list.append({'filename': csv_filename, 'content': final_content})

                success_count += 1

            st.success(f"اكتمل التشفير/التحويل بنجاح. تم تحويل **{success_count}** ملف (إلى TXT و CSV). 🎉")
            status.update(label=f"اكتمل التشفير/التحويل بنجاح. تم تحويل {success_count} ملف. 🎉", state="complete")
            st.session_state.encrypted_files = encrypted_files_list
            st.session_state.txt_files_deleted = False # تأكيد وجود الملفات قبل الحذف
            return encrypted_files_list

        except Exception as e:
            st.error(f"حدث خطأ أثناء التشفير/التحويل: {e}")
            status.update(label=f"حدث خطأ أثناء التشفير/التحويل: {e}", state="error")
            return []

# ----------------------------------------------------------------------

def delete_generated_txt_files_st(status_container):
    """حذف ملفات TXT من الذاكرة (session_state)."""
    
    with status_container.status("بدء عملية حذف ملفات TXT...", expanded=True) as status:
        if st.session_state.txt_files_deleted:
            st.warning("تم حذف ملفات TXT بالفعل في عملية سابقة.")
            status.update(label="ملفات TXT محذوفة بالفعل.", state="error")
            return
            
        initial_count = len([f for f in st.session_state.encrypted_files if f['filename'].endswith('.txt')])
        
        if initial_count == 0:
            st.warning("لم يتم العثور على أي ملفات TXT للحذف في الذاكرة (Session State).")
            status.update(label="لم يتم العثور على ملفات TXT للحذف.", state="error")
            return
            
        
        # تنفيذ عملية الحذف من قائمة الملفات المشفرة في الذاكرة
        try:
            new_encrypted_files = [f for f in st.session_state.encrypted_files if not f['filename'].endswith('.txt')]
            
            deleted_count = initial_count - len([f for f in new_encrypted_files if f['filename'].endswith('.txt')])
            st.session_state.encrypted_files = new_encrypted_files
            st.session_state.txt_files_deleted = True

            st.success(f"اكتمل الحذف بنجاح. تم حذف **{deleted_count}** ملف TXT من الذاكرة. 🗑️")
            status.update(label=f"اكتمل الحذف بنجاح. تم حذف {deleted_count} ملف TXT. 🗑️", state="complete")

        except Exception as e:
            st.error(f"حدث خطأ أثناء عملية الحذف: {e}")
            status.update(label=f"حدث خطأ أثناء عملية الحذف: {e}", state="error")


# ----------------------------------------------------------------------
# --- واجهة Streamlit ---
# ----------------------------------------------------------------------

st.set_page_config(
    page_title="معالج وملف Excel للرواتب",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("معالج وملف Excel للرواتب 💼")
st.markdown("---")

# 1. رفع ملف الإدخال
st.header("1. تحميل ملف الإدخال")
uploaded_file = st.file_uploader(
    "اختر ملف الإكسل (يجب أن يحتوي على الأعمدة: الاسم، Iban، الراتب الصافي)", 
    type=['xlsx', 'xls'],
    key="file_uploader"
)

# حاويات عرض الحالة/النتائج
results_container = st.container()
st.markdown("---")


# 2. المعالجة
st.header("2. بدء المعالجة (إنشاء ملفات Excel مقسمة) 🚀")
process_status_container = st.empty()

if st.button("بدء المعالجة 🚀", key="process_button", disabled=uploaded_file is None):
    with st.spinner("جاري تهيئة المعالجة..."):
        # تشغيل دالة المعالجة وتحديث حالة الجلسة
        process_excel_data_st(uploaded_file, process_status_container)

if st.session_state.processed_files:
    results_container.header("نتائج المعالجة (ملفات Excel)")
    
    # عرض رابط لتحميل جميع الملفات المعالجة في ملف مضغوط (لتجنب عرض الكثير من الأزرار)
    def create_zip_file(files_list):
        from zipfile import ZipFile
        temp_zip_buffer = io.BytesIO()
        with ZipFile(temp_zip_buffer, 'w') as zip_file:
            for file_data in files_list:
                zip_file.writestr(file_data['filename'], file_data['content'])
        return temp_zip_buffer.getvalue()

    zip_content = create_zip_file(st.session_state.processed_files)
    st.download_button(
        label=f"تحميل جميع ملفات الإكسل ({len(st.session_state.processed_files)} ملف) 📥",
        data=zip_content,
        file_name=f"Processed_Excel_Files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
        help="تحميل كافة ملفات الإكسل الناتجة والمقسمة."
    )
    
    with st.expander("معاينة تفاصيل الملفات المعالجة"):
        files_df = pd.DataFrame([
            {'اسم الملف': f['filename'], 'المصرف': f['bank_name'], 'الفرع': f['branch_code'], 'عدد الصفوف': f['rows'], 'المبلغ الإجمالي (د.ع)': f['amount']}
            for f in st.session_state.processed_files
        ])
        st.dataframe(files_df, use_container_width=True)

st.markdown("---")

# 3. إنشاء ملخص الإحصائيات
st.header("3. إنشاء ملخص الإحصائيات 📊")
summary_status_container = st.empty()

if st.button("إنشاء ملخص الإحصائيات 📊", key="summary_button", disabled=not st.session_state.processed_files):
    create_summary_file_st(st.session_state.processed_files, summary_status_container)

if st.session_state.summary_file:
    st.download_button(
        label="تحميل ملف الملخص الإحصائي 📥",
        data=st.session_state.summary_file['content'],
        file_name=st.session_state.summary_file['filename'],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="تحميل ملف الإكسل الذي يحتوي على الملخص الإحصائي الهيكلي."
    )

st.markdown("---")

# 5. تشفير الملفات (TXT/CSV)
st.header("4. تشفير الملفات (تحويل المقسمة إلى TXT/CSV) 🔑")
encryption_status_container = st.empty()

if st.button("تشفير الملفات 🔑", key="encryption_button", disabled=not st.session_state.processed_files):
    batch_convert_excel_to_csv_txt_st(st.session_state.processed_files, encryption_status_container)

if st.session_state.encrypted_files:
    # عرض رابط لتحميل ملفات التشفير (TXT/CSV)
    
    encrypted_zip_content = create_zip_file(st.session_state.encrypted_files)
    st.download_button(
        label=f"تحميل ملفات TXT و CSV المشفرة/المحولة ({len(st.session_state.encrypted_files)} ملف) 📥",
        data=encrypted_zip_content,
        file_name=f"Encrypted_Files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
        help="تحميل كافة الملفات الناتجة بتنسيق TXT و CSV."
    )
    
    with st.expander("معاينة أسماء الملفات المشفرة/المحولة"):
        enc_files_df = pd.DataFrame([{'اسم الملف': f['filename'], 'الحجم': f'{len(f["content"])/1024:.2f} KB'} for f in st.session_state.encrypted_files])
        st.dataframe(enc_files_df, use_container_width=True)

st.markdown("---")

# 6. حذف ملفات TXT المقسمة
st.header("5. حذف ملفات TXT المقسمة ❌")
deletion_status_container = st.empty()
txt_file_count = len([f for f in st.session_state.encrypted_files if f['filename'].endswith('.txt')])

if st.button(f"حذف ملفات TXT المقسمة ({txt_file_count} ملف) ❌", key="delete_txt_button", disabled=txt_file_count == 0):
    # التأكيد في Streamlit يتم عن طريق الأزرار/المكونات وليس رسالة منبثقة
    if st.session_state.txt_files_deleted:
        st.warning("ملفات TXT محذوفة بالفعل.")
    else:
        # هنا يمكن إضافة مربع حوار تأكيد Streamlit، ولكن لتبسيط الأمر نكتفي بزر الحذف المباشر
        delete_generated_txt_files_st(deletion_status_container)
        st.rerun() # إعادة تشغيل التطبيق لعكس حالة الحذف

# ملاحظة حول Streamlit:
# لا تحتاج إلى دالة رئيسية (if __name__ == "__main__": app.mainloop())
# Streamlit يتولى تشغيل الكود وتنفيذ الواجهة. لحفظ هذا الملف، احفظه بصيغة `app.py`
# ثم قم بتشغيله عبر الطرفية باستخدام الأمر: `streamlit run app.py`


