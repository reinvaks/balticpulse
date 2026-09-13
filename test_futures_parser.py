from fetch_futures_snapshot import delivery_start, select_key_gas, implied
assert delivery_start('Sep 2026')=='2026-09-01'
assert delivery_start('Q4 2026')=='2026-10-01'
assert delivery_start('2027')=='2027-01-01'
assert delivery_start('Oct26')=='2026-10-01'
assert delivery_start('Cal 27')=='2027-01-01'
sys=[{'series':'Nordic SYS','tenor':'year','delivery':'2027','delivery_start':'2027-01-01','settlement':50.0,'source_product':'NSBY','open_interest':1,'source_url':''}]
ep=[{'series':'FI EPAD','tenor':'year','delivery':'2027','delivery_start':'2027-01-01','settlement':-2.0,'source_product':'HLBY','open_interest':1,'source_url':''}]
assert implied(sys,ep,'FI implied')[0]['settlement']==48.0
g=[{'contract':'Oct26','delivery_start':'2026-10-01','last':30.0},{'contract':'Q4 26','delivery_start':'2026-10-01','last':31.0},{'contract':'Cal 27','delivery_start':'2027-01-01','last':32.0}]
k=select_key_gas(g); assert {x['horizon'] for x in k}=={'M+1','Q+1','Y+1'}
print('futures parser fixture: OK')
