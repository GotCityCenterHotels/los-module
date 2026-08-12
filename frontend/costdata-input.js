(function () {
    "use strict";
    const API = "/api/costdata/settings";
    const hotel = document.getElementById("settingsHotel"), form = document.getElementById("settingsForm");
    const layout = document.getElementById("settingsLayout"), status = document.getElementById("settingsStatus");
    const errorPanel = document.getElementById("settingsError"), save = document.getElementById("saveSettings");
    const dirtyState = document.getElementById("dirtyState");
    let model = null, dirty = false, loadedEnterpriseId = "";

    const configs = {
        cleaningCategories: [["categoryName","Category","text"],["minGuests","Min guests","number"],["maxGuests","Max guests","number"],["cleaningMinutes","Minutes","number"],["linenCost","Linen cost","number"]],
        arrivalTiers: [["minArrivals","Min arrivals","number"],["maxArrivals","Max arrivals","number"],["receptionHours","Reception hours","number"]],
        breakfastTiers: [["minGuests","Min guests","number"],["maxGuests","Max guests","number"],["staffHours","Staff hours","number"]],
        fixedCosts: [["costName","Cost name","text"],["amount","Amount","number"],["cadence","Cadence","select"],["active","Active","checkbox"]]
    };
    const defaults = { cleaningCategories:{categoryName:"",minGuests:1,maxGuests:"",cleaningMinutes:0,linenCost:0}, arrivalTiers:{minArrivals:0,maxArrivals:"",receptionHours:0}, breakfastTiers:{minGuests:0,maxGuests:"",staffHours:0}, fixedCosts:{costName:"",amount:0,cadence:"monthly",active:true}, distributionGroups:{groupName:"",costPercent:0,rules:[]} };

    async function loadHotels() {
        try { const payload = await LosApi.fetchJson(`${API}/hotels`); for (const property of payload.data || []) hotel.add(new Option(property.hotelName, property.enterpriseId)); }
        catch (error) { showError(error); }
    }
    async function loadSettings(name) {
        if (!name) { layout.hidden = true; return; }
        setBusy(true); errorPanel.hidden = true;
        try { const payload = await LosApi.fetchJson(`${API}/${encodeURIComponent(name)}`); model = payload.data; loadedEnterpriseId = model.enterpriseId; render(); layout.hidden = false; setDirty(false); status.textContent = `Editing ${model.hotelName}`; }
        catch (error) { showError(error); } finally { setBusy(false); }
    }
    function render() {
        for (const [key,value] of Object.entries(model.profile)) { const input=form.elements.namedItem(key); if(input) input.value=value; }
        renderDistribution(); for (const key of Object.keys(configs)) renderRows(key);
    }
    function renderDistribution() {
        const root=document.getElementById("distributionGroups"); root.replaceChildren();
        model.distributionGroups.forEach((group,index)=>{
            const row=document.createElement("div"); row.className="rule-row distribution-rule";
            row.innerHTML=`<div class="rule-main"><label>Group name<input data-field="groupName" value="${escapeHtml(group.groupName)}" required></label><label>Cost %<input data-field="costPercent" type="number" min="0" max="100" step="0.01" value="${group.costPercent}" required></label><button type="button" class="remove-rule" aria-label="Remove group">Remove</button></div><div class="match-list"></div><button type="button" class="text-button add-match">+ Add rate or channel match</button>`;
            row.querySelector(".remove-rule").onclick=()=>removeRow("distributionGroups",index); row.querySelector(".add-match").onclick=()=>{group.rules.push({matchType:"channel",matchValue:""});renderDistribution();setDirty(true)};
            const matches=row.querySelector(".match-list"); group.rules.forEach((rule,ruleIndex)=>{const match=document.createElement("div");match.className="match-row";match.innerHTML=`<select aria-label="Match type"><option value="channel" ${rule.matchType==="channel"?"selected":""}>Channel</option><option value="rate" ${rule.matchType==="rate"?"selected":""}>Rate</option></select><input aria-label="Match value" placeholder="Booking.com, Direct, BAR..." value="${escapeHtml(rule.matchValue)}" required><button type="button" aria-label="Remove match">×</button>`; const [type,value,remove]=match.children;type.onchange=()=>{rule.matchType=type.value;setDirty(true)};value.oninput=()=>{rule.matchValue=value.value;setDirty(true)};remove.onclick=()=>{group.rules.splice(ruleIndex,1);renderDistribution();setDirty(true)};matches.append(match)});
            bindFields(row,group); root.append(row);
        }); emptyMessage(root,"No distribution groups yet.");
    }
    function renderRows(key) { const root=document.getElementById(key);root.replaceChildren();model[key].forEach((item,index)=>{const row=document.createElement("div");row.className="rule-row";for(const [field,label,type] of configs[key]){const wrap=document.createElement("label");wrap.textContent=label;let input;if(type==="select"){input=document.createElement("select");for(const value of ["daily","monthly","yearly"])input.add(new Option(value[0].toUpperCase()+value.slice(1),value));input.value=item[field]}else{input=document.createElement("input");input.type=type; if(type==="number"){input.min="0";input.step="0.01"} if(type==="checkbox")input.checked=item[field];else input.value=item[field]??"";}input.dataset.field=field;wrap.append(input);row.append(wrap)}const remove=document.createElement("button");remove.type="button";remove.className="remove-rule";remove.textContent="Remove";remove.onclick=()=>removeRow(key,index);row.append(remove);bindFields(row,item);root.append(row)});emptyMessage(root,"No rules added yet.") }
    function bindFields(root,item){root.querySelectorAll("[data-field]").forEach(input=>input.addEventListener("input",()=>{item[input.dataset.field]=input.type==="checkbox"?input.checked:input.value;setDirty(true)}))}
    function emptyMessage(root,text){if(!root.children.length){const p=document.createElement("p");p.className="rules-empty";p.textContent=text;root.append(p)}}
    function removeRow(key,index){model[key].splice(index,1);key==="distributionGroups"?renderDistribution():renderRows(key);setDirty(true)}
    function escapeHtml(value){return String(value??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;")}
    function collect(){for(const input of form.querySelectorAll("[name]"))model.profile[input.name]=input.value;return model}
    async function submit(event){event.preventDefault();if(!form.reportValidity())return;setBusy(true);errorPanel.hidden=true;try{const payload=await LosApi.fetchJson(`${API}/${encodeURIComponent(hotel.value)}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(collect())});model=payload.data;loadedEnterpriseId=model.enterpriseId;render();setDirty(false);status.textContent=`Saved ${model.hotelName}`;}catch(error){showError(error)}finally{setBusy(false)}}
    function setDirty(value){dirty=value;dirtyState.textContent=value?"Unsaved changes":"No unsaved changes";dirtyState.classList.toggle("is-dirty",value)}
    function setBusy(value){save.disabled=value;hotel.disabled=value;document.querySelector(".settings-workspace").setAttribute("aria-busy",String(value))}
    function showError(error){errorPanel.textContent=error.message||"Unable to load cost settings.";errorPanel.hidden=false;status.textContent="Something went wrong."}
    document.querySelectorAll(".settings-nav button").forEach(button=>button.onclick=()=>{document.querySelectorAll(".settings-nav button").forEach(x=>x.removeAttribute("aria-current"));button.setAttribute("aria-current","page");document.querySelectorAll("[data-settings-section]").forEach(section=>section.hidden=section.dataset.settingsSection!==button.dataset.section)});
    document.querySelectorAll("[data-add]").forEach(button=>button.onclick=()=>{const key=button.dataset.add;model[key].push(structuredClone(defaults[key]));key==="distributionGroups"?renderDistribution():renderRows(key);setDirty(true)});
    hotel.onchange=()=>{if(dirty&&!confirm("Discard unsaved changes?")){hotel.value=loadedEnterpriseId;return}loadSettings(hotel.value)};form.addEventListener("input",()=>setDirty(true));form.onsubmit=submit;window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue=""}});loadHotels();
}());
