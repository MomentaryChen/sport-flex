
//過濾API回傳的資料陣列，WebClient 沒有DataTable 欄位且會回傳陣列，WebService 會有DataTable 欄位
function generateDataArray(data) {


    if (!Object.hasOwn(data,'DataTable')) {
        return data;
    } 
    
    if (data.DataTable && data.DataTable.DataRow) {
        return Array.isArray(data.DataTable.DataRow) ? data.DataTable.DataRow : [data.DataTable.DataRow];
    } 

    return [];
    
}