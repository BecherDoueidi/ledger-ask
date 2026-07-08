
CREATE DATABASE IF NOT EXISTS CharityAIAgentFullDemoDB;

USE CharityAIAgentFullDemoDB;

/* =========================
   MASTER TABLES
========================= */

CREATE TABLE Donors (
    DonorId INT AUTO_INCREMENT PRIMARY KEY,
    FullName VARCHAR(200),
    Mobile VARCHAR(20),
    Email VARCHAR(200),
    Nationality VARCHAR(100),
    RegistrationDate DATE,
    Status VARCHAR(50)
);

CREATE TABLE CorporateDonors (
    CorporateId INT AUTO_INCREMENT PRIMARY KEY,
    CompanyName VARCHAR(200),
    TradeLicenseNo VARCHAR(100),
    ContactPerson VARCHAR(200),
    Mobile VARCHAR(20),
    Email VARCHAR(200),
    Emirate VARCHAR(100),
    AnnualContribution DECIMAL(18,2),
    Status VARCHAR(50)
);

CREATE TABLE Beneficiaries (
    BeneficiaryId INT AUTO_INCREMENT PRIMARY KEY,
    FullName VARCHAR(200),
    Mobile VARCHAR(20),
    Nationality VARCHAR(100),
    Emirate VARCHAR(100),
    FamilyMembers INT,
    MonthlyIncome DECIMAL(18,2),
    Status VARCHAR(50),
    RegistrationDate DATE
);

CREATE TABLE BeneficiaryFamilyMembers (
    MemberId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    FullName VARCHAR(200),
    Relation VARCHAR(100),
    Age INT,
    MonthlyIncome DECIMAL(18,2),
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId)
);

CREATE TABLE BeneficiaryDocuments (
    DocumentId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    DocumentType VARCHAR(100),
    UploadDate DATE,
    ExpiryDate DATE NULL,
    Status VARCHAR(50),
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId)
);

CREATE TABLE CharityPrograms (
    ProgramId INT AUTO_INCREMENT PRIMARY KEY,
    ProgramName VARCHAR(200),
    ProgramCategory VARCHAR(150),
    Description VARCHAR(500),
    IsActive TINYINT(1)
);

CREATE TABLE Campaigns (
    CampaignId INT AUTO_INCREMENT PRIMARY KEY,
    ProgramId INT,
    CampaignName VARCHAR(200),
    TargetAmount DECIMAL(18,2),
    CollectedAmount DECIMAL(18,2),
    StartDate DATE,
    EndDate DATE,
    Status VARCHAR(50),
    FOREIGN KEY (ProgramId) REFERENCES CharityPrograms(ProgramId)
);

CREATE TABLE DonationChannels (
    ChannelId INT AUTO_INCREMENT PRIMARY KEY,
    ChannelName VARCHAR(100)
);

CREATE TABLE Donations (
    DonationId INT AUTO_INCREMENT PRIMARY KEY,
    DonorId INT NULL,
    CorporateId INT NULL,
    CampaignId INT,
    ChannelId INT,
    DonationAmount DECIMAL(18,2),
    DonationDate DATE,
    PaymentMethod VARCHAR(50),
    ReceiptNumber VARCHAR(100),
    PaymentStatus VARCHAR(50),
    FOREIGN KEY (DonorId) REFERENCES Donors(DonorId),
    FOREIGN KEY (CorporateId) REFERENCES CorporateDonors(CorporateId),
    FOREIGN KEY (CampaignId) REFERENCES Campaigns(CampaignId),
    FOREIGN KEY (ChannelId) REFERENCES DonationChannels(ChannelId)
);

CREATE TABLE AssistanceRequests (
    RequestId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    ProgramId INT,
    RequestType VARCHAR(150),
    RequestedAmount DECIMAL(18,2),
    RequestDate DATE,
    Status VARCHAR(50),
    Priority VARCHAR(50),
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId),
    FOREIGN KEY (ProgramId) REFERENCES CharityPrograms(ProgramId)
);

CREATE TABLE AssistancePayments (
    PaymentId INT AUTO_INCREMENT PRIMARY KEY,
    RequestId INT,
    ApprovedAmount DECIMAL(18,2),
    PaymentDate DATE,
    PaymentStatus VARCHAR(50),
    FOREIGN KEY (RequestId) REFERENCES AssistanceRequests(RequestId)
);

CREATE TABLE Sponsorships (
    SponsorshipId INT AUTO_INCREMENT PRIMARY KEY,
    DonorId INT,
    BeneficiaryId INT,
    MonthlyAmount DECIMAL(18,2),
    StartDate DATE,
    EndDate DATE NULL,
    Status VARCHAR(50),
    FOREIGN KEY (DonorId) REFERENCES Donors(DonorId),
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId)
);

CREATE TABLE Branches (
    BranchId INT AUTO_INCREMENT PRIMARY KEY,
    BranchName VARCHAR(150),
    Emirate VARCHAR(100)
);

CREATE TABLE DonationLocations (
    LocationId INT AUTO_INCREMENT PRIMARY KEY,
    LocationName VARCHAR(250),
    Emirate VARCHAR(100),
    WorkingHours VARCHAR(100),
    IsActive TINYINT(1)
);

CREATE TABLE UrgentCases (
    CaseId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    ProgramId INT,
    CaseTitle VARCHAR(200),
    RequiredAmount DECIMAL(18,2),
    CollectedAmount DECIMAL(18,2),
    CaseStatus VARCHAR(50),
    CreatedDate DATE,
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId),
    FOREIGN KEY (ProgramId) REFERENCES CharityPrograms(ProgramId)
);

CREATE TABLE InKindSupport (
    SupportId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    ProgramId INT,
    SupportType VARCHAR(100),
    Quantity INT,
    EstimatedValue DECIMAL(18,2),
    SupportDate DATE,
    Status VARCHAR(50),
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId),
    FOREIGN KEY (ProgramId) REFERENCES CharityPrograms(ProgramId)
);

CREATE TABLE Volunteers (
    VolunteerId INT AUTO_INCREMENT PRIMARY KEY,
    FullName VARCHAR(200),
    Mobile VARCHAR(20),
    Email VARCHAR(200),
    Skills VARCHAR(500),
    JoinDate DATE,
    Status VARCHAR(50)
);

CREATE TABLE VolunteerActivities (
    ActivityId INT AUTO_INCREMENT PRIMARY KEY,
    VolunteerId INT,
    ActivityName VARCHAR(200),
    ActivityDate DATE,
    HoursWorked DECIMAL(5,2),
    Location VARCHAR(200),
    FOREIGN KEY (VolunteerId) REFERENCES Volunteers(VolunteerId)
);

CREATE TABLE FundraisingEvents (
    EventId INT AUTO_INCREMENT PRIMARY KEY,
    EventName VARCHAR(200),
    EventDate DATE,
    Location VARCHAR(200),
    TargetAmount DECIMAL(18,2),
    RaisedAmount DECIMAL(18,2),
    Status VARCHAR(50)
);

CREATE TABLE EventDonations (
    EventDonationId INT AUTO_INCREMENT PRIMARY KEY,
    EventId INT,
    DonorId INT,
    DonationAmount DECIMAL(18,2),
    DonationDate DATE,
    FOREIGN KEY (EventId) REFERENCES FundraisingEvents(EventId),
    FOREIGN KEY (DonorId) REFERENCES Donors(DonorId)
);

CREATE TABLE Orphans (
    OrphanId INT AUTO_INCREMENT PRIMARY KEY,
    FullName VARCHAR(200),
    Gender VARCHAR(20),
    DateOfBirth DATE,
    Country VARCHAR(100),
    SponsorStatus VARCHAR(50)
);

CREATE TABLE OrphanSponsors (
    OrphanSponsorshipId INT AUTO_INCREMENT PRIMARY KEY,
    OrphanId INT,
    DonorId INT,
    MonthlyAmount DECIMAL(18,2),
    StartDate DATE,
    EndDate DATE NULL,
    Status VARCHAR(50),
    FOREIGN KEY (OrphanId) REFERENCES Orphans(OrphanId),
    FOREIGN KEY (DonorId) REFERENCES Donors(DonorId)
);

CREATE TABLE MedicalCases (
    MedicalCaseId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    Diagnosis VARCHAR(500),
    HospitalName VARCHAR(200),
    RequestedAmount DECIMAL(18,2),
    ApprovedAmount DECIMAL(18,2),
    Status VARCHAR(50),
    CreatedDate DATE,
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId)
);

CREATE TABLE StudentSupport (
    StudentId INT AUTO_INCREMENT PRIMARY KEY,
    BeneficiaryId INT,
    SchoolName VARCHAR(200),
    AcademicYear VARCHAR(20),
    TuitionFees DECIMAL(18,2),
    ApprovedAmount DECIMAL(18,2),
    Status VARCHAR(50),
    FOREIGN KEY (BeneficiaryId) REFERENCES Beneficiaries(BeneficiaryId)
);

/* =========================
   SAMPLE DATA
========================= */

INSERT INTO CharityPrograms (ProgramName, ProgramCategory, Description, IsActive) VALUES
('Zakat', 'Financial Assistance', 'Zakat donations and distribution to eligible families.', 1),
('Alms', 'Financial Assistance', 'General sadaqah and alms donations.', 1),
('Ramadan Meer', 'Ramadan Projects', 'Ramadan food baskets and family support.', 1),
('Fasting Project', 'Ramadan Projects', 'Iftar meals and fasting support.', 1),
('Medical Treatment', 'Social Support', 'Medical treatment support for eligible cases.', 1),
('Food For All', 'Social Support', 'Meal and food support.', 1),
('Food Scheme', 'Social Support', 'Monthly food supplies for families.', 1),
('Orphan Funds', 'Sponsorship', 'Support for orphans.', 1),
('Student Project', 'Education', 'Education support for students.', 1),
('House Maintenance', 'Social Support', 'Maintenance support for low-income houses.', 1),
('Emergency Aid', 'Emergency Support', 'Urgent financial assistance cases.', 1),
('Waqf Endowment', 'Waqf', 'Endowment donations and charity shares.', 1),
('People of Determination', 'Social Support', 'Support for people of determination.', 1),
('Debtors Fund', 'Financial Assistance', 'Support for debtors and hardship cases.', 1),
('Prisoners Families', 'Social Support', 'Support for families of prisoners.', 1);

INSERT INTO DonationChannels (ChannelName) VALUES
('Website'), ('Mobile App'), ('SMS'), ('Bank Card'), ('Bank Transfer'),
('Donation Counter'), ('Call Center'), ('Corporate Donation');

INSERT INTO Donors (FullName, Mobile, Email, Nationality, RegistrationDate, Status) VALUES
('Ahmed Ali', '0501112223', 'ahmed.ali@email.com', 'UAE', '2024-01-10', 'Active'),
('Fatima Hassan', '0502223334', 'fatima@email.com', 'UAE', '2024-02-05', 'Active'),
('Mohammed Omar', '0503334445', 'mohammed@email.com', 'Egypt', '2024-03-12', 'Active'),
('Sara Khalid', '0504445556', 'sara@email.com', 'Sudan', '2024-04-18', 'Active'),
('Khalid Saeed', '0505556661', 'khalid@email.com', 'UAE', '2025-01-15', 'Active'),
('Mona Ahmed', '0505556662', 'mona@email.com', 'UAE', '2025-02-20', 'Active'),
('Yousef Nasser', '0505556663', 'yousef@email.com', 'Jordan', '2025-03-11', 'Active'),
('Huda Salem', '0505556664', 'huda@email.com', 'UAE', '2025-04-05', 'Active'),
('Omar Al Mansoori', '0505556665', 'omar@email.com', 'UAE', '2026-01-09', 'Active'),
('Aisha Noor', '0505556666', 'aisha@email.com', 'Syria', '2026-02-14', 'Active'),
('Salem Rashid', '0505556667', 'salem@email.com', 'UAE', '2026-03-05', 'Active'),
('Reem Abdullah', '0505556668', 'reem@email.com', 'UAE', '2026-04-10', 'Active');

INSERT INTO CorporateDonors (CompanyName, TradeLicenseNo, ContactPerson, Mobile, Email, Emirate, AnnualContribution, Status) VALUES
('Al Noor Holding', 'TL-10001', 'Nasser Salem', '0521001001', 'csr@alnoor.com', 'Dubai', 250000, 'Active'),
('Green Crescent Trading', 'TL-10002', 'Mariam Ali', '0521001002', 'csr@greencrescent.com', 'Sharjah', 180000, 'Active'),
('Emirates Food Industries', 'TL-10003', 'Hamad Khalid', '0521001003', 'csr@efi.com', 'Abu Dhabi', 320000, 'Active'),
('Future Tech Solutions', 'TL-10004', 'Sara Mansour', '0521001004', 'csr@futuretech.com', 'Dubai', 120000, 'Active');

INSERT INTO Beneficiaries (FullName, Mobile, Nationality, Emirate, FamilyMembers, MonthlyIncome, Status, RegistrationDate) VALUES
('Hassan Mahmoud', '0551112223', 'Sudan', 'Dubai', 5, 2500, 'Active', '2024-01-15'),
('Aisha Ibrahim', '0552223334', 'Syria', 'Sharjah', 3, 1800, 'Active', '2024-02-10'),
('Omar Yousif', '0553334445', 'Egypt', 'Ajman', 4, 3000, 'Inactive', '2024-03-05'),
('Mariam Saleh', '0554445556', 'UAE', 'Dubai', 6, 2200, 'Active', '2024-04-12'),
('Noor Abdullah', '0555551001', 'UAE', 'Fujairah', 4, 2000, 'Active', '2025-01-10'),
('Yasin Ahmed', '0555551002', 'Sudan', 'Dubai', 6, 1500, 'Active', '2025-02-13'),
('Salma Omar', '0555551003', 'Syria', 'Ras Al Khaimah', 3, 1700, 'Active', '2025-03-21'),
('Kareem Mostafa', '0555551004', 'Egypt', 'Ajman', 5, 2300, 'Active', '2026-01-18'),
('Rana Mahmoud', '0555551005', 'Jordan', 'Dubai', 2, 2800, 'Active', '2026-02-25'),
('Ali Hassan', '0555551006', 'UAE', 'Sharjah', 7, 1900, 'Active', '2026-03-28');

INSERT INTO BeneficiaryFamilyMembers (BeneficiaryId, FullName, Relation, Age, MonthlyIncome) VALUES
(1, 'Mona Hassan', 'Wife', 35, 0),
(1, 'Ali Hassan', 'Son', 10, 0),
(2, 'Lina Ibrahim', 'Daughter', 8, 0),
(4, 'Khalid Saleh', 'Son', 12, 0),
(5, 'Maryam Abdullah', 'Daughter', 9, 0),
(6, 'Ahmed Yasin', 'Son', 7, 0),
(8, 'Nour Kareem', 'Daughter', 6, 0),
(10, 'Fatima Ali', 'Daughter', 11, 0);

INSERT INTO BeneficiaryDocuments (BeneficiaryId, DocumentType, UploadDate, ExpiryDate, Status) VALUES
(1, 'Emirates ID', '2024-01-16', '2026-01-16', 'Valid'),
(1, 'Salary Certificate', '2024-01-16', NULL, 'Valid'),
(2, 'Passport', '2024-02-11', '2027-02-11', 'Valid'),
(4, 'Tenancy Contract', '2024-04-13', '2025-04-13', 'Expired'),
(5, 'Medical Report', '2025-01-12', NULL, 'Valid'),
(6, 'Family Book', '2025-02-15', NULL, 'Missing'),
(8, 'Bank Statement', '2026-01-20', NULL, 'Valid'),
(10, 'Court Document', '2026-03-30', NULL, 'Valid');

INSERT INTO Campaigns (ProgramId, CampaignName, TargetAmount, CollectedAmount, StartDate, EndDate, Status) VALUES
(3, 'Ramadan Campaign 2024', 250000, 240000, '2024-03-01', '2024-04-10', 'Completed'),
(1, 'Zakat Campaign 2024', 300000, 285000, '2024-01-01', '2024-12-31', 'Completed'),
(9, 'Education Support 2024', 180000, 160000, '2024-08-01', '2024-09-30', 'Completed'),
(6, 'Food For All 2024', 120000, 118000, '2024-11-01', '2024-12-31', 'Completed'),
(3, 'Ramadan Campaign 2025', 350000, 330000, '2025-02-15', '2025-03-31', 'Completed'),
(5, 'Medical Support 2025', 220000, 175000, '2025-01-01', '2025-12-31', 'Completed'),
(8, 'Orphan Happiness 2025', 260000, 245000, '2025-01-01', '2025-12-31', 'Completed'),
(3, 'Ramadan Campaign 2026', 400000, 315000, '2026-02-15', '2026-03-31', 'Active'),
(11, 'Emergency Aid 2026', 300000, 210000, '2026-01-01', '2026-12-31', 'Active'),
(12, 'Waqf Endowment 2026', 500000, 180000, '2026-01-01', '2026-12-31', 'Active');

INSERT INTO Donations (DonorId, CorporateId, CampaignId, ChannelId, DonationAmount, DonationDate, PaymentMethod, ReceiptNumber, PaymentStatus) VALUES
(1, NULL, 1, 1, 10000, '2024-03-05', 'Card', 'REC-2024-001', 'Paid'),
(2, NULL, 1, 2, 15000, '2024-03-10', 'Bank Transfer', 'REC-2024-002', 'Paid'),
(3, NULL, 2, 1, 20000, '2024-04-01', 'Card', 'REC-2024-003', 'Paid'),
(4, NULL, 3, 6, 8000, '2024-08-20', 'Cash', 'REC-2024-004', 'Paid'),
(NULL, 1, 4, 8, 50000, '2024-11-15', 'Corporate Donation', 'REC-2024-005', 'Paid'),
(5, NULL, 5, 1, 25000, '2025-03-01', 'Card', 'REC-2025-001', 'Paid'),
(6, NULL, 5, 5, 12000, '2025-03-05', 'Bank Transfer', 'REC-2025-002', 'Paid'),
(7, NULL, 6, 4, 18000, '2025-06-18', 'Card', 'REC-2025-003', 'Paid'),
(NULL, 2, 7, 8, 85000, '2025-09-22', 'Corporate Donation', 'REC-2025-004', 'Paid'),
(8, NULL, 6, 1, 7000, '2025-11-10', 'Card', 'REC-2025-005', 'Paid'),
(9, NULL, 8, 2, 30000, '2026-02-20', 'Card', 'REC-2026-001', 'Paid'),
(10, NULL, 8, 1, 15000, '2026-03-05', 'Bank Transfer', 'REC-2026-002', 'Paid'),
(NULL, 3, 9, 8, 95000, '2026-04-15', 'Corporate Donation', 'REC-2026-003', 'Paid'),
(11, NULL, 10, 1, 10000, '2026-05-01', 'Card', 'REC-2026-004', 'Pending'),
(12, NULL, 9, 1, 5000, '2026-06-01', 'Card', 'REC-2026-005', 'Paid');

INSERT INTO AssistanceRequests (BeneficiaryId, ProgramId, RequestType, RequestedAmount, RequestDate, Status, Priority) VALUES
(1, 5, 'Medical Treatment', 7000, '2024-04-20', 'Approved', 'High'),
(2, 9, 'Education Support', 5000, '2024-05-01', 'Pending', 'Medium'),
(3, 6, 'Food Support', 3000, '2024-05-10', 'Rejected', 'Low'),
(4, 10, 'House Maintenance', 10000, '2024-06-01', 'Approved', 'High'),
(5, 11, 'Emergency Aid', 12000, '2025-02-15', 'Approved', 'High'),
(6, 5, 'Medical Treatment', 15000, '2025-07-10', 'Approved', 'High'),
(7, 14, 'Debtors Fund', 20000, '2025-09-05', 'Pending', 'High'),
(8, 13, 'People of Determination Support', 8000, '2026-01-20', 'Approved', 'High'),
(9, 7, 'Food Scheme', 4500, '2026-03-12', 'Pending', 'Medium'),
(10, 15, 'Prisoners Families Support', 9000, '2026-04-18', 'Approved', 'Medium');

INSERT INTO AssistancePayments (RequestId, ApprovedAmount, PaymentDate, PaymentStatus) VALUES
(1, 6500, '2024-04-25', 'Paid'),
(4, 9500, '2024-06-10', 'Paid'),
(5, 11000, '2025-02-20', 'Paid'),
(6, 14500, '2025-07-15', 'Paid'),
(8, 7500, '2026-01-25', 'Paid'),
(10, 8500, '2026-04-25', 'Paid');

INSERT INTO Sponsorships (DonorId, BeneficiaryId, MonthlyAmount, StartDate, EndDate, Status) VALUES
(1, 1, 1000, '2024-01-01', '2024-12-31', 'Completed'),
(2, 2, 1500, '2024-03-01', '2024-12-31', 'Completed'),
(5, 5, 1200, '2025-01-01', '2025-12-31', 'Completed'),
(6, 6, 1800, '2025-02-01', '2025-12-31', 'Completed'),
(9, 8, 2000, '2026-01-01', '2026-12-31', 'Active'),
(10, 9, 1500, '2026-02-01', '2026-12-31', 'Active');

INSERT INTO Branches (BranchName, Emirate) VALUES
('Dubai Branch', 'Dubai'),
('Fujairah Branch', 'Fujairah'),
('Ras Al Khaimah Branch', 'Ras Al Khaimah'),
('Ajman Branch', 'Ajman'),
('Hatta Branch', 'Dubai');

INSERT INTO DonationLocations (LocationName, Emirate, WorkingHours, IsActive) VALUES
('Al Nahda Main Office', 'Dubai', 'Sunday to Friday 7 AM - 11 PM', 1),
('Al Barsha Donation Counter', 'Dubai', 'Sunday to Friday 7 AM - 11 PM', 1),
('Deira City Centre Counter', 'Dubai', 'Sunday to Friday 7 AM - 11 PM', 1),
('Mirdif City Centre Counter', 'Dubai', 'Sunday to Friday 7 AM - 11 PM', 1),
('Fujairah Branch Counter', 'Fujairah', 'Sunday to Friday 7 AM - 11 PM', 1),
('Ajman Branch Counter', 'Ajman', 'Sunday to Friday 7 AM - 11 PM', 1);

INSERT INTO UrgentCases (BeneficiaryId, ProgramId, CaseTitle, RequiredAmount, CollectedAmount, CaseStatus, CreatedDate) VALUES
(1, 5, 'Emergency Medical Surgery', 25000, 18000, 'Open', '2024-04-01'),
(2, 11, 'Family Rent Support', 15000, 15000, 'Closed', '2025-02-01'),
(6, 5, 'Cancer Treatment Support', 40000, 28000, 'Open', '2025-07-01'),
(8, 11, 'Emergency Home Repair', 30000, 21000, 'Open', '2026-01-15'),
(10, 14, 'Debt Settlement Case', 50000, 32000, 'Open', '2026-04-10');

INSERT INTO InKindSupport (BeneficiaryId, ProgramId, SupportType, Quantity, EstimatedValue, SupportDate, Status) VALUES
(1, 7, 'Food Basket', 2, 600, '2024-03-20', 'Delivered'),
(2, 3, 'Ramadan Meal', 30, 900, '2024-03-25', 'Delivered'),
(5, 9, 'Laptop For Student', 1, 2500, '2025-09-01', 'Delivered'),
(6, 6, 'Monthly Food Package', 3, 1200, '2025-11-10', 'Delivered'),
(8, 13, 'Wheelchair', 1, 3000, '2026-02-01', 'Delivered'),
(9, 7, 'Food Basket', 4, 1400, '2026-03-18', 'Pending');

INSERT INTO Volunteers (FullName, Mobile, Email, Skills, JoinDate, Status) VALUES
('Hessa Mohammed', '0561001001', 'hessa@email.com', 'Event coordination, data entry', '2024-01-15', 'Active'),
('Abdullah Salem', '0561001002', 'abdullah@email.com', 'Logistics, delivery', '2024-03-10', 'Active'),
('Noura Khalid', '0561001003', 'noura@email.com', 'Call center, beneficiary support', '2025-02-20', 'Active'),
('Yara Ahmed', '0561001004', 'yara@email.com', 'Social media, campaign support', '2025-05-12', 'Active'),
('Omar Hamdan', '0561001005', 'omarh@email.com', 'Warehouse, food distribution', '2026-01-18', 'Active');

INSERT INTO VolunteerActivities (VolunteerId, ActivityName, ActivityDate, HoursWorked, Location) VALUES
(1, 'Ramadan Food Distribution', '2024-03-15', 6, 'Dubai'),
(2, 'Donation Counter Support', '2024-04-05', 4, 'Dubai'),
(3, 'Beneficiary Call Campaign', '2025-03-20', 5, 'Sharjah'),
(4, 'Education Campaign Support', '2025-09-01', 7, 'Dubai'),
(5, 'Emergency Aid Distribution', '2026-02-10', 8, 'Ajman'),
(1, 'Waqf Awareness Event', '2026-05-01', 5, 'Dubai');

INSERT INTO FundraisingEvents (EventName, EventDate, Location, TargetAmount, RaisedAmount, Status) VALUES
('Ramadan Giving Night 2024', '2024-03-20', 'Dubai', 100000, 95000, 'Completed'),
('Education Support Gala 2024', '2024-09-10', 'Dubai', 80000, 72000, 'Completed'),
('Orphan Happiness Event 2025', '2025-05-12', 'Sharjah', 120000, 110000, 'Completed'),
('Medical Aid Campaign Event 2025', '2025-10-08', 'Dubai', 150000, 130000, 'Completed'),
('Waqf Charity Forum 2026', '2026-04-20', 'Dubai', 200000, 145000, 'Active');

INSERT INTO EventDonations (EventId, DonorId, DonationAmount, DonationDate) VALUES
(1, 1, 10000, '2024-03-20'),
(1, 2, 15000, '2024-03-20'),
(2, 3, 12000, '2024-09-10'),
(3, 5, 25000, '2025-05-12'),
(4, 6, 18000, '2025-10-08'),
(5, 9, 30000, '2026-04-20'),
(5, 11, 15000, '2026-04-20');

INSERT INTO Orphans (FullName, Gender, DateOfBirth, Country, SponsorStatus) VALUES
('Amna Yusuf', 'Female', '2014-05-12', 'UAE', 'Sponsored'),
('Bilal Omar', 'Male', '2013-08-22', 'Sudan', 'Sponsored'),
('Layla Ahmad', 'Female', '2016-02-14', 'Syria', 'Not Sponsored'),
('Hassan Ali', 'Male', '2015-11-30', 'Egypt', 'Sponsored'),
('Mariam Khalid', 'Female', '2012-07-18', 'Jordan', 'Not Sponsored');

INSERT INTO OrphanSponsors (OrphanId, DonorId, MonthlyAmount, StartDate, EndDate, Status) VALUES
(1, 1, 800, '2024-01-01', '2024-12-31', 'Completed'),
(2, 2, 900, '2024-03-01', '2024-12-31', 'Completed'),
(4, 5, 1000, '2025-01-01', '2025-12-31', 'Completed'),
(1, 9, 1200, '2026-01-01', NULL, 'Active'),
(2, 10, 1100, '2026-02-01', NULL, 'Active');

INSERT INTO MedicalCases (BeneficiaryId, Diagnosis, HospitalName, RequestedAmount, ApprovedAmount, Status, CreatedDate) VALUES
(1, 'Heart surgery support', 'Dubai Hospital', 25000, 20000, 'Approved', '2024-04-01'),
(5, 'Cancer treatment support', 'Rashid Hospital', 40000, 35000, 'Approved', '2025-07-01'),
(6, 'Dialysis treatment', 'Al Qassimi Hospital', 30000, 25000, 'Pending', '2025-09-15'),
(8, 'Emergency surgery', 'Ajman Specialty Hospital', 18000, 15000, 'Approved', '2026-01-20'),
(10, 'Physical therapy support', 'Dubai Rehabilitation Center', 12000, 9000, 'Pending', '2026-04-10');

INSERT INTO StudentSupport (BeneficiaryId, SchoolName, AcademicYear, TuitionFees, ApprovedAmount, Status) VALUES
(2, 'Al Noor School', '2024-2025', 12000, 10000, 'Approved'),
(4, 'Dubai National School', '2024-2025', 15000, 12000, 'Approved'),
(5, 'Fujairah Private School', '2025-2026', 14000, 11000, 'Approved'),
(7, 'RAK Academy', '2025-2026', 16000, 13000, 'Pending'),
(9, 'Dubai Modern School', '2026-2027', 18000, 15000, 'Pending');
