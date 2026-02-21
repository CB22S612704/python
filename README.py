import time
import openpyxl
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By





driver = webdriver.Chrome()
driver.get("https://pvpm.practicevelocity.com/26_1/LogBook.aspx")
driver.maximize_window()    
time.sleep(5)

Username='MAGESHR@ZAFHQ'
PassWORD='Mg@lone$4580'   

driver.find_element(By.XPATH,"//*[@id='txtLogin']").send_keys(Username)
driver.find_element(By.XPATH,"//*[@id='btnNext']").click()
driver.find_element(By.XPATH,"//*[@id='okta-signin-password']").send_keys(PassWORD)  
driver.find_element(By.XPATH,"//*[@id='okta-signin-submit']").click()    
time.sleep(20)

excel_path = r"C:\Users\mages\Desktop\Practice.xlsx" 
wb = openpyxl.load_workbook(excel_path)
sheet = wb.active

for row in range(2, sheet.max_row + 1):
    a_value = sheet[f"A{row}"].value
    b_value = sheet[f"B{row}"].value

    if a_value is None:
        continue
    driver.find_element(By.XPATH,"//*[@id='tdMenuBarItemAdministration']/a").click()
    driver.find_element(By.XPATH,"//*[@id='menu_Administration_Reports']").click()
    time.sleep(10)

    driver.find_element(By.NAME, "userSearch").click().send_key(a_value)
    
