# 📊 Smart Attendance System

<p align="center">
  <strong>A smart, secure, and modern attendance management system</strong>
</p>

<p align="center">
  Manage students, track attendance, and automate attendance marking using Face Recognition and QR Code technology.
</p>

<p align="center">
  <a href="https://smart-attendance-system-blue-seven.vercel.app/login">
    <img src="https://img.shields.io/badge/%F0%9F%9A%80%20Live%20Demo-Visit%20App-success?style=for-the-badge" alt="Live Demo">
  </a>
  <a href="https://github.com/Vinay9686803463/smart-attendance-system">
    <img src="https://img.shields.io/badge/%F0%9F%92%BB%20Repository-GitHub-black?style=for-the-badge&logo=github" alt="GitHub Repository">
  </a>
</p>

---

## 🛠️ Tech Stack

<p align="center">
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white" alt="Flask">
  <img src="https://img.shields.io/badge/OpenCV-5C3EE8?style=for-the-badge&logo=opencv&logoColor=white" alt="OpenCV">
  <img src="https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite">
  <img src="https://img.shields.io/badge/HTML5-E34F26?style=for-the-badge&logo=html5&logoColor=white" alt="HTML5">
  <img src="https://img.shields.io/badge/CSS3-1572B6?style=for-the-badge&logo=css3&logoColor=white" alt="CSS3">
  <img src="https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge&logo=javascript&logoColor=black" alt="JavaScript">
  <img src="https://img.shields.io/badge/Vercel-000000?style=for-the-badge&logo=vercel&logoColor=white" alt="Vercel">
</p>

---

## ✨ Features

- 👨‍🎓 **Student Management** — Add, edit, search, and manage student records.
- 👨‍🏫 **Role-Based Authentication** — Separate Faculty and Student access.
- 📝 **Attendance Management** — Mark and manage daily attendance.
- 📷 **Face Recognition** — Automate attendance using facial recognition.
- 📱 **QR Code Attendance** — Mark attendance using QR codes.
- 📊 **Attendance History** — View student attendance records and statistics.
- 📈 **Attendance Reports** — Track present, absent, and attendance percentages.
- 🔐 **Secure Authentication** — Login, password reset, OTP verification, and sessions.
- ☁️ **Cloud Deployment** — Deployable using Vercel.
- 📱 **Responsive UI** — Designed to work across desktop and mobile screens.

---

## 📁 Project Structure

```text
smart-attendance-system/
│
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
│
├── templates/
│   ├── login.html
│   ├── dashboard.html
│   ├── students.html
│   ├── add_student.html
│   ├── attendance.html
│   ├── attendance_records.html
│   └── ...
│
├── static/
│   ├── css/
│   ├── js/
│   ├── images/
│   └── faces/
│
├── screenshots/
│   ├── login.png
│   ├── dashboard.png
│   ├── attendance.png
│   └── students.png
│
└── attendance.db
```

> **Note:** `attendance.db` is used for local development. For production, use a persistent external database such as PostgreSQL/Supabase.

---

## 🔑 Default Sign-in

A default Faculty account is automatically created when using a fresh database.

| Field | Default |
|---|---|
| **Role** | Faculty |
| **Username** | `admin` |
| **Password** | `admin123` |

> ⚠️ **Security:** Change the default password immediately after your first login.

You can customize the default account using environment variables:

```text
ADMIN_USERNAME
ADMIN_PASSWORD
ADMIN_EMAIL
```

---

## 💻 Run Locally

### 1. Clone the repository

```bash
git clone https://github.com/Vinay9686803463/smart-attendance-system.git
cd smart-attendance-system
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the application

```bash
python app.py
```

### 4. Open the application

```text
http://127.0.0.1:5000/login
```

---

## 🚀 Deploy on Vercel

1. Push the project to the `main` branch.
2. Import the repository into Vercel.
3. Configure the required environment variables.
4. Deploy the application.

Required secret:

```text
ATTENDANCE_SECRET_KEY
```

### ⚠️ Database Consideration

SQLite is suitable for local development, but a serverless deployment should use a persistent external database for production data.

For production, the application can be connected to:

```text
PostgreSQL
     │
     ▼
  Supabase
     │
     ▼
Smart Attendance System
```

This allows student records and attendance history to persist independently of serverless function instances.

---

## 🌐 Project Links

| Resource | Link |
|---|---|
| 🚀 **Live Demo** | [Smart Attendance Live Demo](https://smart-attendance-system-blue-seven.vercel.app/login) |
| 💻 **GitHub Repository** | [Smart Attendance System Repository](https://github.com/Vinay9686803463/smart-attendance-system) |

---

## 🔮 Future Improvements

- ☁️ Persistent Supabase/PostgreSQL database
- 📧 Email notifications
- 📊 Advanced attendance analytics
- 📄 PDF attendance reports
- 📱 Progressive Web App support
- 🔔 Automated attendance notifications
- 👥 Multiple faculty/admin management

---

## 👨‍💻 Author

**Vinay9686803463**

Built as a full-stack web application to explore **Flask, database management, authentication, computer vision, and cloud deployment**.

---

<p align="center">
  ⭐ If you find this project useful, consider giving it a star on GitHub!
</p>